"""Create (or update) the question-counter table and the login the web API counts with.

    python -m ingestion.create_limits_role

Run it once per database, as the app's own login (POSTGRES_USER), like
create_reader_role. The login's name and password come from POSTGRES_LIMITS_USER
and POSTGRES_LIMITS_PASSWORD. Running it again updates the password.

The web API needs to write exactly one thing: how many questions each visitor and
the whole site have asked in the current hour and day. This login may read and
change that table and nothing else, so the reader login stays read-only.
"""
import os
import re
import sys

import psycopg
from psycopg import sql

from ingestion import database

STATEMENT_TIMEOUT = "5s"

# visitor is a keyed hash of the visitor's IP address (never the address itself),
# or "site" for the site-wide count. Rows older than two days are deleted.
CREATE_TABLE = """
    CREATE TABLE IF NOT EXISTS question_counts (
        visitor TEXT NOT NULL,
        period TEXT NOT NULL CHECK (period IN ('hour', 'day')),
        window_start TIMESTAMPTZ NOT NULL,
        count INT NOT NULL CHECK (count >= 0),
        PRIMARY KEY (visitor, period, window_start)
    )
"""


def create_limits_role(conn: psycopg.Connection, name: str, password: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"Limits role name {name!r} must be lower-case letters, digits and _")
    if not password:
        raise ValueError("Limits role password must not be empty")
    role = sql.Identifier(name)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CREATE_TABLE)
        exists = cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
        verb = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
        cur.execute(sql.SQL(verb).format(role, sql.Literal(password)))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(STATEMENT_TIMEOUT)))
        db = cur.execute("SELECT current_database()").fetchone()[0]
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON question_counts TO {}").format(role))


def main(argv: list) -> int:
    if argv:
        print("Usage: python -m ingestion.create_limits_role", file=sys.stderr)
        return 2
    name = os.environ.get("POSTGRES_LIMITS_USER")
    password = os.environ.get("POSTGRES_LIMITS_PASSWORD")
    if not name or not password:
        print("Set POSTGRES_LIMITS_USER and POSTGRES_LIMITS_PASSWORD first.", file=sys.stderr)
        return 1
    try:
        with database.connect() as conn:
            create_limits_role(conn, name, password)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"Creating the limits role failed: {error}", file=sys.stderr)
        return 1
    print(f"Limits role {name} is ready: it may only read and change question_counts.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
