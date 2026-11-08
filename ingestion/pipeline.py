"""Ingest one Discogs label: its vinyl releases and today's snapshot of each.

Composes the tested pieces: the paginated label list, release details, the
catalogue save and the snapshot save. Prices are fetched every night; a stored
release's details (want, have, ratings and the catalogue fields) once a week,
on a weekday of its own, so each night fetches about a seventh of them. A failed release is logged and listed in
the result, and the run carries on with the others; only losing the database
connection stops the run, since every later save would fail too.
"""
import decimal
import logging
from dataclasses import dataclass, field
from datetime import date, timedelta

import psycopg
import requests

from ingestion import config
from ingestion.discogs_client import get_label_releases, get_marketplace_stats, get_release
from ingestion.mapping import MappingError, is_clearly_not_vinyl, is_vinyl
from ingestion.repository import (
    details_fetched_on, forget_skip, get_saved_snapshot, remember_skip, remembered_skip, save_price_snapshot,
    save_release, save_snapshots,
)

log = logging.getLogger(__name__)


# Errors that belong to one release: Discogs failing, data the catalogue cannot
# use (including malformed answers, such as an artist without a name), or the
# database refusing a row. Each is recorded and the run carries on.
RELEASE_ERRORS = (
    requests.RequestException, MappingError, psycopg.Error,
    KeyError, TypeError, AttributeError, decimal.InvalidOperation,
)

# What ingest_release found, besides a saved snapshot.
NOT_VINYL = "not vinyl"
NOT_ON_LABEL = "not on label"


# Details older than this are fetched whatever the weekday, so a release whose
# night was missed (a failure, the computer off) catches up on the next one.
DETAILS_MAX_AGE = timedelta(days=7)


class DatabaseConnectionLost(Exception):
    """The database connection broke during the run, so the run was stopped."""


@dataclass
class LabelRun:
    label_id: int
    listed: int = 0
    saved: list = field(default_factory=list)
    not_vinyl: list = field(default_factory=list)
    not_on_label: list = field(default_factory=list)
    gone: list = field(default_factory=list)
    # Of the saved releases, those whose details were fetched as well as prices.
    with_details: list = field(default_factory=list)
    failed: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return not self.failed


def unique_releases(summaries: list) -> list:
    """Label lists can name the same release more than once; keep the first."""
    seen = {}
    for summary in summaries:
        seen.setdefault(summary["id"], summary)
    return list(seen.values())


def describe_snapshot(saved, snapshot_date: date = None) -> str:
    num_for_sale, lowest_price, currency, want, have, rating_count, rating_average, details_date = saved
    supply = f"{num_for_sale} for sale"
    if lowest_price is not None:
        supply += f" from {lowest_price} {currency}"
    rating = f"rated {rating_average} by {rating_count}" if rating_average is not None else "not rated"
    details = f"want {want}, have {have}; {rating}"
    if details_date is not None and details_date != snapshot_date:
        details += f" (details from {details_date})"
    return f"{supply}; {details}"


def details_due(release_id: int, fetched_on, snapshot_date: date) -> bool:
    """Whether tonight fetches the release's details, not only its price.

    A release not stored yet always needs them. A stored one gets them on its own
    weekday (from its id, so the catalogue is spread over the week), or as soon as
    they are DETAILS_MAX_AGE old.
    """
    if fetched_on is None:
        return True
    if snapshot_date - fetched_on >= DETAILS_MAX_AGE:
        return True
    return release_id % 7 == snapshot_date.weekday() and fetched_on < snapshot_date


def is_on_label(discogs_release: dict, label_id: int) -> bool:
    """The release was issued on the label, not merely credited to it.

    A label's Discogs list also holds releases where the name only appears in a
    credit, such as a studio under "Recorded At". Those credits are listed as
    companies; the release's own labels are what it was issued on.
    """
    return any(label.get("id") == label_id for label in discogs_release.get("labels") or [])


def ingest_release(conn: psycopg.Connection, release_id: int, snapshot_date: date, label_id: int = None):
    """Fetch, check and save one release with its snapshot. Returns the saved snapshot,
    NOT_ON_LABEL when label_id is given and the release was not issued on it, or
    NOT_VINYL when the details show it is not vinyl."""
    if label_id is not None and not details_due(
        release_id, details_fetched_on(conn, release_id, label_id), snapshot_date
    ):
        # A stored vinyl release of this label whose details are recent: price only.
        stats = get_marketplace_stats(release_id, config.SNAPSHOT_CURRENCY)
        save_price_snapshot(conn, release_id, stats, snapshot_date)
        with conn.transaction():
            return get_saved_snapshot(conn, release_id, snapshot_date)
    discogs_release = get_release(release_id)
    if label_id is not None and not is_on_label(discogs_release, label_id):
        return NOT_ON_LABEL
    if not is_vinyl(discogs_release.get("formats") or []):
        return NOT_VINYL
    stats = get_marketplace_stats(release_id, config.SNAPSHOT_CURRENCY)
    save_release(conn, discogs_release)
    save_snapshots(conn, release_id, stats, discogs_release, snapshot_date)
    # Read inside its own transaction: a read left open would turn every later
    # save of the run into a savepoint, committed only when the run ends.
    with conn.transaction():
        return get_saved_snapshot(conn, release_id, snapshot_date)


def ingest_label(conn: psycopg.Connection, label_id: int, snapshot_date: date) -> LabelRun:
    """Ingest every vinyl release of a label. Errors in the label list itself are raised."""
    run = LabelRun(label_id)
    summaries = unique_releases(get_label_releases(label_id))
    run.listed = len(summaries)
    log.info("Label %s: %s releases listed", label_id, run.listed)

    for number, summary in enumerate(summaries, start=1):
        release_id = summary["id"]
        step = f"[{number}/{run.listed}] {release_id}"
        if is_clearly_not_vinyl(summary.get("format")):
            run.not_vinyl.append(release_id)
            log.info("%s skipped, not vinyl (%s)", step, summary.get("format"))
            continue
        remembered = remembered_skip(conn, release_id, label_id, snapshot_date)
        if remembered == NOT_ON_LABEL:
            run.not_on_label.append(release_id)
            log.info("%s skipped, only credited to label %s (remembered)", step, label_id)
            continue
        if remembered == NOT_VINYL:
            run.not_vinyl.append(release_id)
            log.info("%s skipped, not vinyl (remembered)", step)
            continue
        try:
            saved = ingest_release(conn, release_id, snapshot_date, label_id)
        except requests.HTTPError as error:
            if error.response is not None and error.response.status_code == 404:
                # Still on the label's list, but Discogs no longer has the release.
                run.gone.append(release_id)
                log.warning("%s gone: Discogs no longer has this release", step)
                continue
            run.failed[release_id] = str(error)
            log.error("%s failed: %s", step, error)
            continue
        except RELEASE_ERRORS as error:
            run.failed[release_id] = f"{type(error).__name__}: {error}"
            log.error("%s failed: %s: %s", step, type(error).__name__, error)
            if conn.broken:
                raise DatabaseConnectionLost(str(error)) from error
            continue
        if saved == NOT_ON_LABEL:
            run.not_on_label.append(release_id)
            remember_skip(conn, release_id, label_id, NOT_ON_LABEL, snapshot_date)
            log.info("%s skipped, only credited to label %s, not issued on it", step, label_id)
        elif saved == NOT_VINYL:
            run.not_vinyl.append(release_id)
            remember_skip(conn, release_id, label_id, NOT_VINYL, snapshot_date)
            log.info("%s skipped, not vinyl (details)", step)
        else:
            run.saved.append(release_id)
            if saved[-1] == snapshot_date:
                run.with_details.append(release_id)
                # Details were fetched, so a release skipped before may have been corrected.
                forget_skip(conn, release_id, label_id)
            log.info("%s saved: %s", step, describe_snapshot(saved, snapshot_date))

    return run
