"""Create any missing tables from db/schema.sql in an existing database.

    python -m ingestion.apply_schema

Docker applies db/schema.sql only when the database is first created. Every
statement in it is CREATE ... IF NOT EXISTS, so running it again adds tables
introduced since then (for example tracked_labels) and leaves existing tables
and their data untouched. The few changes to existing tables, such as the
warning column and the run status check, are written so they can run again too.
"""
import sys
from pathlib import Path

import psycopg

from ingestion import database

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"


def apply_schema(conn: psycopg.Connection) -> None:
    with conn.transaction():
        conn.execute(SCHEMA.read_text())


def main(argv: list) -> int:
    if argv:
        print("Usage: python -m ingestion.apply_schema", file=sys.stderr)
        return 2
    try:
        with database.connect() as conn:
            apply_schema(conn)
    except psycopg.Error as error:
        print(f"Applying the schema failed: {error}", file=sys.stderr)
        return 1
    print("Schema applied: every table in db/schema.sql exists.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
