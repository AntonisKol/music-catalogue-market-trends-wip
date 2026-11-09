"""Create any missing tables from db/schema.sql in an existing database.

    python -m ingestion.apply_schema

Docker applies db/schema.sql only when the database is first created. Every
statement in it is CREATE ... IF NOT EXISTS, ADD COLUMN IF NOT EXISTS, or an
INSERT that skips rows already there (the first label of each stored release
into release_labels), so running it again adds what was introduced since then
(for example tracked_labels) and leaves existing data untouched.
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
