"""Fetch one Discogs release and save it to PostgreSQL.

    python -m ingestion.save_release 249504
"""
import sys

import psycopg
import requests

from ingestion import database
from ingestion.discogs_client import get_release
from ingestion.mapping import MappingError
from ingestion.repository import get_saved_release, save_release


def main(argv: list) -> int:
    if len(argv) != 1 or not argv[0].isdigit():
        print("Usage: python -m ingestion.save_release <discogs_release_id>", file=sys.stderr)
        return 2
    release_id = int(argv[0])

    try:
        discogs_release = get_release(release_id)
    except requests.RequestException as error:
        print(f"Discogs request for release {release_id} failed: {error}", file=sys.stderr)
        return 1

    try:
        with database.connect() as conn:
            save_release(conn, discogs_release)
            saved = get_saved_release(conn, release_id)
    except MappingError as error:
        print(f"Release {release_id} could not be mapped: {error}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"Saving release {release_id} to PostgreSQL failed: {error}", file=sys.stderr)
        return 1

    _, title, year, label, artists = saved
    print(f"Saved {release_id}: {', '.join(artists)} - {title} ({year or 'no year'}) on {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
