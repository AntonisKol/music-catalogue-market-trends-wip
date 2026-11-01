"""Fetch today's market and community data for one release and save a snapshot.

    python -m ingestion.save_snapshot 249504

The release itself is saved (or updated) first, since snapshots reference it.
"Today" is the UTC date, so the day doesn't depend on where the job runs.
"""
import sys
from datetime import datetime, timezone

import psycopg
import requests

from ingestion import config, database
from ingestion.discogs_client import get_marketplace_stats, get_release
from ingestion.mapping import MappingError
from ingestion.pipeline import describe_snapshot
from ingestion.repository import get_saved_snapshot, save_release, save_snapshots


def main(argv: list) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("Usage: python -m ingestion.save_snapshot <discogs_release_id>", file=sys.stderr)
        return 2
    release_id = int(argv[0])
    snapshot_date = datetime.now(timezone.utc).date()

    try:
        discogs_release = get_release(release_id)
        stats = get_marketplace_stats(release_id, config.SNAPSHOT_CURRENCY)
    except requests.RequestException as error:
        print(f"Discogs request for release {release_id} failed: {error}", file=sys.stderr)
        return 1

    try:
        with database.connect() as conn:
            save_release(conn, discogs_release)
            save_snapshots(conn, release_id, stats, discogs_release, snapshot_date)
            saved = get_saved_snapshot(conn, release_id, snapshot_date)
    except MappingError as error:
        print(f"Release {release_id} could not be mapped: {error}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"Saving the snapshot of release {release_id} failed: {error}", file=sys.stderr)
        return 1

    print(f"Snapshot of {release_id} on {snapshot_date} (UTC): {describe_snapshot(saved)}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
