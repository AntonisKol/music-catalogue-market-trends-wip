"""Ingest a Discogs label: save its vinyl releases and today's snapshot of each.

    python -m ingestion.ingest_label 1

Progress is logged with timestamps to stderr. Exits 0 when every vinyl release
was saved, 1 when any failed (they are listed at the end) or the run could not
finish, and 2 on wrong usage. Running it again the same day updates the same rows.
"""
import logging
import sys
from datetime import datetime, timezone

import psycopg
import requests

from ingestion import database
from ingestion.pipeline import DatabaseConnectionLost, ingest_label

log = logging.getLogger("ingestion.ingest_label")


def main(argv: list) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("Usage: python -m ingestion.ingest_label <discogs_label_id>", file=sys.stderr)
        return 2
    label_id = int(argv[0])
    snapshot_date = datetime.now(timezone.utc).date()
    log.info("Ingesting label %s, snapshot date %s (UTC)", label_id, snapshot_date)

    try:
        with database.connect() as conn:
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
        "Label %s done: %s listed, %s saved, %s not vinyl, %s failed",
        label_id, run.listed, len(run.saved), len(run.not_vinyl), len(run.failed),
    )
    for release_id, error in run.failed.items():
        log.error("Failed release %s: %s", release_id, error)
    return 0 if run.ok else 1


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    sys.exit(main(sys.argv[1:]))
