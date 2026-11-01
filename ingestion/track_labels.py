"""Manage the labels the nightly run updates.

    python -m ingestion.track_labels list
    python -m ingestion.track_labels add 1 538     # Discogs label ids
    python -m ingestion.track_labels pause 538     # skip it at night, keep its data
    python -m ingestion.track_labels resume 538

Adding a label checks it on Discogs and records its name. Its data arrives with
the next nightly run, or at once with: python -m ingestion.ingest_label <id>
"""
import sys

import psycopg
import requests

from ingestion import database
from ingestion.discogs_client import get_label

USAGE = "Usage: python -m ingestion.track_labels list | add <id>... | pause <id>... | resume <id>..."


def add_label(conn: psycopg.Connection, label_id: int, added_by: str = "site") -> str:
    """Track a label (again, if it was paused). Returns its name. Discogs errors are raised."""
    name = get_label(label_id)["name"]
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO tracked_labels (label_id, name, added_by) VALUES (%s, %s, %s)
            ON CONFLICT (label_id) DO UPDATE SET name = EXCLUDED.name, active = true
            """,
            (label_id, name, added_by),
        )
    return name


def set_active(conn: psycopg.Connection, label_id: int, active: bool) -> bool:
    """Pause or resume a tracked label. Returns False if it is not tracked."""
    with conn.transaction():
        row = conn.execute(
            "UPDATE tracked_labels SET active = %s WHERE label_id = %s RETURNING label_id", (active, label_id)
        ).fetchone()
    return row is not None


def tracked_labels(conn: psycopg.Connection, active_only: bool = False) -> list:
    with conn.transaction():
        return conn.execute(
            "SELECT label_id, name, active, added_by, added_at FROM tracked_labels"
            + (" WHERE active" if active_only else "") + " ORDER BY name, label_id"
        ).fetchall()


def main(argv: list) -> int:
    if not argv or argv[0] not in {"list", "add", "pause", "resume"}:
        print(USAGE, file=sys.stderr)
        return 2
    command, ids = argv[0], argv[1:]
    if (command == "list") != (not ids) or not all(i.isdigit() for i in ids):
        print(USAGE, file=sys.stderr)
        return 2
    status = 0
    try:
        with database.connect() as conn:
            if command == "list":
                rows = tracked_labels(conn)
                if not rows:
                    print("No labels are tracked yet. Add one with: python -m ingestion.track_labels add <id>")
                for label_id, name, active, added_by, added_at in rows:
                    print(f"{label_id:>8}  {name}  ({'active' if active else 'paused'}, added by {added_by} on {added_at:%Y-%m-%d})")
            for label_id in map(int, ids):
                if command == "add":
                    try:
                        print(f"Tracking {label_id}: {add_label(conn, label_id)}")
                    except requests.HTTPError as error:
                        missing = error.response is not None and error.response.status_code == 404
                        print(f"Label {label_id}: {'not found on Discogs' if missing else error}", file=sys.stderr)
                        status = 1
                elif set_active(conn, label_id, command == "resume"):
                    print(f"Label {label_id} {'resumed' if command == 'resume' else 'paused'}.")
                else:
                    print(f"Label {label_id} is not tracked.", file=sys.stderr)
                    status = 1
    except requests.RequestException as error:
        print(f"Discogs could not be reached: {error}", file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"Database error: {error}", file=sys.stderr)
        return 1
    return status


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
