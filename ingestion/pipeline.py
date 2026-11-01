"""Ingest one Discogs label: its vinyl releases and today's snapshot of each.

Composes the tested pieces: the paginated label list, release details, the
catalogue save and the snapshot save. A failed release is logged and listed in
the result, and the run carries on with the others; only losing the database
connection stops the run, since every later save would fail too.
"""
import logging
from dataclasses import dataclass, field
from datetime import date

import psycopg
import requests

from ingestion import config
from ingestion.discogs_client import get_label_releases, get_marketplace_stats, get_release
from ingestion.mapping import MappingError, is_clearly_not_vinyl, is_vinyl
from ingestion.repository import get_saved_snapshot, save_release, save_snapshots

log = logging.getLogger(__name__)


class DatabaseConnectionLost(Exception):
    """The database connection broke during the run, so the run was stopped."""


@dataclass
class LabelRun:
    label_id: int
    listed: int = 0
    saved: list = field(default_factory=list)
    not_vinyl: list = field(default_factory=list)
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


def describe_snapshot(saved) -> str:
    num_for_sale, lowest_price, currency, want, have, rating_count, rating_average = saved
    supply = f"{num_for_sale} for sale"
    if lowest_price is not None:
        supply += f" from {lowest_price} {currency}"
    rating = f"rated {rating_average} by {rating_count}" if rating_average is not None else "not rated"
    return f"{supply}; want {want}, have {have}; {rating}"


def ingest_release(conn: psycopg.Connection, release_id: int, snapshot_date: date):
    """Fetch, check and save one release with its snapshot. Returns the saved snapshot,
    or None when the details show it is not vinyl."""
    discogs_release = get_release(release_id)
    if not is_vinyl(discogs_release.get("formats") or []):
        return None
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
        try:
            saved = ingest_release(conn, release_id, snapshot_date)
        except (requests.RequestException, MappingError, psycopg.Error) as error:
            run.failed[release_id] = str(error)
            log.error("%s failed: %s", step, error)
            if conn.broken:
                raise DatabaseConnectionLost(str(error)) from error
            continue
        if saved is None:
            run.not_vinyl.append(release_id)
            log.info("%s skipped, not vinyl (details)", step)
        else:
            run.saved.append(release_id)
            log.info("%s saved: %s", step, describe_snapshot(saved))

    return run
