"""Show the recent nightly runs and anything that went wrong.

    python -m ingestion.runs          # the last 10 runs
    python -m ingestion.runs 30
"""
import sys

import psycopg

from ingestion import database


def recent_runs(conn: psycopg.Connection, limit: int) -> list:
    with conn.transaction():
        return conn.execute(
            """
            SELECT r.id, r.snapshot_date, r.started_at, r.finished_at, r.status, r.message,
                   count(l.label_id), coalesce(sum(l.saved), 0), coalesce(sum(l.failed), 0),
                   count(l.error)
            FROM ingestion_runs r LEFT JOIN ingestion_run_labels l ON l.run_id = r.id
            GROUP BY r.id ORDER BY r.started_at DESC LIMIT %s
            """,
            (limit,),
        ).fetchall()


def main(argv: list) -> int:
    if len(argv) > 1 or (argv and not argv[0].isdigit()):
        print("Usage: python -m ingestion.runs [how many]", file=sys.stderr)
        return 2
    try:
        with database.connect() as conn:
            rows = recent_runs(conn, int(argv[0]) if argv else 10)
    except psycopg.Error as error:
        print(f"Database error: {error}", file=sys.stderr)
        return 1
    if not rows:
        print("No nightly runs recorded yet.")
    for run_id, day, started, finished, status, message, labels, saved, failed, label_errors in rows:
        took = f"{(finished - started).total_seconds() / 60:.0f} min" if finished else "still running"
        print(f"{started:%Y-%m-%d %H:%M} UTC  {status:<9}  {labels} labels, {saved} saved, {failed} failed, "
              f"{label_errors} label errors  ({took})")
        if status == "failed" and message:
            print(f"    {message}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
