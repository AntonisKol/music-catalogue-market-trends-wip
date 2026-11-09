"""Ingest a Discogs label: save its vinyl releases and today's snapshot of each.

    python -m ingestion.ingest_label 1
    python -m ingestion.ingest_label --wait 1   # waits for a running import instead of exiting

Progress is logged with timestamps to stderr. Exits 0 when every vinyl release
was saved, 1 when any failed (they are listed at the end), the run could not
finish or (without --wait) another run is in progress, and 2 on wrong usage. Releases Discogs no
longer has are listed as gone and do not count as failures. Running it again the
same day updates the same rows.
"""
import logging
import sys
import time
from datetime import datetime, timezone

import psycopg
import requests

from ingestion import database
from ingestion.pipeline import DatabaseConnectionLost, ingest_label

log = logging.getLogger("ingestion.ingest_label")

# Two runs at once would each pace their own requests and together exceed
# Discogs' rate limit. The first run holds this PostgreSQL advisory lock; it is
# released when its connection closes, also after a crash.
RUN_LOCK_ID = 2026110101
# With --wait, how often to check whether the running import has finished.
WAIT_SECONDS = 60


def take_run_lock(conn: psycopg.Connection) -> bool:
    # In its own transaction, so no transaction stays open for the run's saves.
    with conn.transaction():
        return conn.execute("SELECT pg_try_advisory_lock(%s)", (RUN_LOCK_ID,)).fetchone()[0]


def wait_for_run_lock(conn: psycopg.Connection) -> None:
    # Polled rather than a blocking pg_advisory_lock, so no statement stays open for hours.
    if take_run_lock(conn):
        return
    log.info("Another ingestion run is in progress; waiting for it to finish")
    while not take_run_lock(conn):
        time.sleep(WAIT_SECONDS)
    log.info("The other run has finished; starting")


def main(argv: list) -> int:
    wait = argv[:1] == ["--wait"]
    if wait:
        argv = argv[1:]
    if len(argv) != 1 or not argv[0].isdigit():
        print("Usage: python -m ingestion.ingest_label [--wait] <discogs_label_id>", file=sys.stderr)
        return 2
    label_id = int(argv[0])
    snapshot_date = datetime.now(timezone.utc).date()
    log.info("Ingesting label %s, snapshot date %s (UTC)", label_id, snapshot_date)

    try:
        with database.connect() as conn:
            if wait:
                wait_for_run_lock(conn)
            elif not take_run_lock(conn):
                log.error("Another ingestion run is in progress; not starting a second one")
                return 1
            run = ingest_label(conn, label_id, snapshot_date)
    except requests.RequestException as error:
        log.error("Discogs request for label %s failed: %s", label_id, error)
        return 1
    except DatabaseConnectionLost as error:
        log.error("Stopped: the database connection was lost: %s", error)
        return 1
    except psycopg.Error as error:
        log.error("Could not connect to PostgreSQL: %s", error)
        return 1

    log.info(
        "Label %s done: %s listed, %s saved, %s not vinyl, %s not on this label, %s gone, %s failed",
        label_id, run.listed, len(run.saved), len(run.not_vinyl), len(run.not_on_label),
        len(run.gone), len(run.failed),
    )
    for release_id in run.gone:
        log.warning("Gone from Discogs: release %s", release_id)
    for release_id, error in run.failed.items():
        log.error("Failed release %s: %s", release_id, error)
    return 0 if run.ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main(sys.argv[1:]))
