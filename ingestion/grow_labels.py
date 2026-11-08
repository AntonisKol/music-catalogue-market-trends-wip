"""Add the most-collected techno labels, up to ten a night, while the nightly run has room.

    python -m ingestion.grow_labels         # after the nightly update (deploy/nightly.sh)
    python -m ingestion.grow_labels list    # every label looked at, and why it was added or left out

Discogs allows 60 requests a minute, so the nightly run can't price every techno label
every night. The owner prefers fewer labels with complete nightly data, so labels are
only added while the next night is projected to stay under BUDGET_HOURS, and the most
collected come first. Added labels have added_by = 'nightly'; the next nightly run
imports them, and the page lists a label once its releases are in the database.

Ranking: Discogs' search sorted by how many people have each techno vinyl release, as
far as it goes (10,000 releases). A label scores one point for each of them it released
(the first label named). Going down that ranking, a label is added when:
- Discogs has a label with exactly that name;
- at least a third of its vinyl releases are tagged Techno (so not Warp, Mute or Virgin);
- it has at most MAX_VINYL vinyl releases;
- nobody's request for it was rejected, and it isn't tracked already (a paused label stays paused);
- the projected night stays under BUDGET_HOURS. If not, nothing more is added that night.

The projection is the last finished nightly run's time per tracked release, times the
tracked releases plus the new label's vinyl releases. A night that imported new labels
takes longer than later ones, so the next projection errs on the side of fewer labels.
Every label looked at is kept in label_candidates with the reason; left-out labels are
looked at again after RECHECK_DAYS, and labels with no room on the next night.
"""
import logging
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone

import psycopg
import requests

from ingestion import database, discogs_client
from ingestion.ingest_label import take_run_lock
from ingestion.nightly import configure_logging
from ingestion.track_labels import add_label
from web.label_requests import comparable

log = logging.getLogger("ingestion.grow_labels")

LABELS_A_NIGHT = 10
BUDGET_HOURS = 20
MIN_TECHNO_SHARE = 1 / 3
MAX_VINYL = 2000
RECHECK_DAYS = 90
SEARCH_PAGES = 100  # of 100 releases: as far as Discogs' search goes
USAGE = "Usage: python -m ingestion.grow_labels [list]"
REASONS = {
    "added": "added",
    "not_techno": "fewer than a third of its vinyl releases are techno",
    "too_big": f"more than {MAX_VINYL} vinyl releases",
    "not_found": "no Discogs label with exactly this name",
    "tracked": "already tracked, or paused by the owner",
    "rejected": "a visitor's request for it was rejected",
    "no_room": f"the nightly run would pass {BUDGET_HOURS} hours",
}


def run_speed(conn: psycopg.Connection):
    """(seconds per tracked release, releases the next night will update) from the last
    finished nightly run, or None. Labels added here whose releases aren't in the database
    yet (not imported, or the import failed) count with their vinyl releases from Discogs."""
    with conn.transaction():
        last = conn.execute("SELECT extract(epoch FROM finished_at - started_at) FROM ingestion_runs"
                            " WHERE finished_at IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        tracked = conn.execute("SELECT count(*) FROM releases r JOIN tracked_labels t ON t.label_id = r.label_id"
                               " WHERE t.active").fetchone()[0]
        pending = conn.execute(
            "SELECT coalesce(sum(c.vinyl), 0) FROM label_candidates c"
            " JOIN tracked_labels t ON t.label_id = c.label_id AND t.active"
            " WHERE c.verdict = 'added' AND NOT EXISTS (SELECT 1 FROM releases r WHERE r.label_id = c.label_id)"
        ).fetchone()[0]
    if last is None or not tracked:
        return None
    return float(last[0]) / tracked, tracked + int(pending)


def ranked_labels() -> list:
    """Label names, most-collected first: how many of the most-had techno vinyl releases each put out."""
    score = Counter()
    for page in range(1, SEARCH_PAGES + 1):
        data = discogs_client.search_releases(page=page, style="Techno", format="Vinyl", sort="have",
                                              sort_order="desc")
        for release in data["results"]:
            if release.get("label"):
                score[release["label"][0]] += 1
        if page >= data["pagination"]["pages"]:
            break
    return [name for name, _ in score.most_common()]


def exact_label(name: str):
    """The Discogs label id with exactly this name, or None."""
    return next((match["id"] for match in discogs_client.search_labels(name, limit=10)
                 if match["title"].casefold() == name.casefold()), None)


def remember(conn, name: str, verdict: str, now: datetime, label_id=None, vinyl=None, techno=None) -> None:
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO label_candidates (name, label_id, vinyl, techno, verdict, checked_at)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (name) DO UPDATE SET label_id = EXCLUDED.label_id, vinyl = EXCLUDED.vinyl,
                techno = EXCLUDED.techno, verdict = EXCLUDED.verdict, checked_at = EXCLUDED.checked_at
            """,
            (name, label_id, vinyl, techno, verdict, now),
        )
    log.info("%s: %s", name, REASONS[verdict])


def grow(conn: psycopg.Connection, now: datetime) -> list:
    """Add up to LABELS_A_NIGHT labels while there is room. Returns the names added."""
    speed = run_speed(conn)
    if speed is None:
        log.info("No finished nightly run to measure yet; no labels added.")
        return []
    seconds_per_release, planned = speed
    budget = BUDGET_HOURS * 3600
    log.info("Last run: %.2f s per release, %s releases tracked, about %.1f of %s hours.",
             seconds_per_release, planned, seconds_per_release * planned / 3600, BUDGET_HOURS)
    if seconds_per_release * planned >= budget:
        log.info("The nightly run is at its ceiling; no labels added.")
        return []

    with conn.transaction():
        tracked_ids = {row[0] for row in conn.execute("SELECT label_id FROM tracked_labels")}
        tracked_names = {comparable(row[0]) for row in conn.execute("SELECT name FROM tracked_labels")}
        rejected = {comparable(row[0]) for row in
                    conn.execute("SELECT name FROM label_requests WHERE status = 'rejected'")}
        recent = {row[0] for row in conn.execute(
            "SELECT name FROM label_candidates WHERE checked_at > %s AND verdict <> 'no_room'",
            (now - timedelta(days=RECHECK_DAYS),))}

    added = []
    for name in ranked_labels():
        if len(added) == LABELS_A_NIGHT:
            break
        if comparable(name) in tracked_names or name in recent:
            continue
        if comparable(name) in rejected:
            remember(conn, name, "rejected", now)
            continue
        label_id = exact_label(name)
        if label_id is None:
            remember(conn, name, "not_found", now)
            continue
        if label_id in tracked_ids:
            remember(conn, name, "tracked", now, label_id)
            continue
        # The name filter also finds similar names, so the vinyl count is capped at the label's exact size.
        vinyl = min(discogs_client.count_releases(label=name, format="Vinyl"), discogs_client.label_size(label_id))
        techno = min(discogs_client.count_releases(label=name, format="Vinyl", style="Techno"), vinyl)
        if vinyl > MAX_VINYL:
            remember(conn, name, "too_big", now, label_id, vinyl, techno)
        elif not vinyl or techno / vinyl < MIN_TECHNO_SHARE:
            remember(conn, name, "not_techno", now, label_id, vinyl, techno)
        elif seconds_per_release * (planned + vinyl) > budget:
            remember(conn, name, "no_room", now, label_id, vinyl, techno)
            break
        else:
            title = add_label(conn, label_id, added_by="nightly")
            remember(conn, name, "added", now, label_id, vinyl, techno)
            planned += vinyl
            added.append(title)
    log.info("Added %s label(s): %s. Next night projected at about %.1f hours.",
             len(added), ", ".join(added) or "none", seconds_per_release * planned / 3600)
    return added


def show(conn: psycopg.Connection) -> None:
    with conn.transaction():
        rows = conn.execute("SELECT name, label_id, vinyl, techno, verdict, checked_at FROM label_candidates"
                            " ORDER BY checked_at DESC, name").fetchall()
    if not rows:
        print("No labels looked at yet.")
    for name, label_id, vinyl, techno, verdict, checked in rows:
        counts = f"  {techno}/{vinyl} techno vinyl" if vinyl is not None else ""
        print(f"{checked:%Y-%m-%d}  {name}  ({label_id or 'no id'}){counts}: {REASONS[verdict]}")


def main(argv: list) -> int:
    if argv not in ([], ["list"]):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        with database.connect() as conn:
            if argv == ["list"]:
                show(conn)
                return 0
            configure_logging()
            if not take_run_lock(conn):
                log.error("Another ingestion run holds the lock; no labels added tonight.")
                return 1
            grow(conn, datetime.now(timezone.utc))
            return 0
    except requests.RequestException as error:
        log.error("Discogs could not be reached; no more labels added tonight: %s", error)
    except psycopg.Error as error:
        log.error("Database error; no more labels added tonight: %s", error)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
