"""Create (or update) the read-only PostgreSQL login used by the question feature.

    python -m ingestion.create_reader_role

Run it once per database, as the app's own login (POSTGRES_USER, which owns the
tables). The reader's name and password come from POSTGRES_READER_USER and
POSTGRES_READER_PASSWORD. Running it again updates the password and settings.

The reader may only SELECT, now and on tables added later. The database also
opens every one of its sessions read-only and stops any query after 5 seconds.
"""
import os
import re
import sys

import psycopg
from psycopg import sql

from ingestion import database

STATEMENT_TIMEOUT = "5s"


def create_reader_role(conn: psycopg.Connection, name: str, password: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"Reader role name {name!r} must be lower-case letters, digits and _")
    if not password:
        raise ValueError("Reader role password must not be empty")
    role = sql.Identifier(name)
    with conn.transaction(), conn.cursor() as cur:
        owner = cur.execute("SELECT current_user").fetchone()[0]
        exists = cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
        # CREATE/ALTER ROLE take no query parameters, so the password is quoted as a literal.
        verb = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
        cur.execute(sql.SQL(verb).format(role, sql.Literal(password)))
        cur.execute(sql.SQL("ALTER ROLE {} SET default_transaction_read_only = on").format(role))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(STATEMENT_TIMEOUT)))
        db = cur.execute("SELECT current_database()").fetchone()[0]
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        cur.execute(sql.SQL("GRANT SELECT ON ALL TABLES IN SCHEMA public TO {}").format(role))
        cur.execute(
            sql.SQL("ALTER DEFAULT PRIVILEGES FOR ROLE {} IN SCHEMA public GRANT SELECT ON TABLES TO {}").format(
                sql.Identifier(owner), role
            )
        )


def main(argv: list) -> int:
    if argv:
        print("Usage: python -m ingestion.create_reader_role", file=sys.stderr)
        return 2
    name = os.environ.get("POSTGRES_READER_USER")
    password = os.environ.get("POSTGRES_READER_PASSWORD")
    if not name or not password:
        print("Set POSTGRES_READER_USER and POSTGRES_READER_PASSWORD first.", file=sys.stderr)
        return 1
    try:
        with database.connect() as conn:
            create_reader_role(conn, name, password)
    except ValueError as error:
        print(error, file=sys.stderr)
        return 1
    except psycopg.Error as error:
        print(f"Creating the reader role failed: {error}", file=sys.stderr)
        return 1
    print(f"Reader role {name} is ready: SELECT only, read-only sessions, {STATEMENT_TIMEOUT} query limit.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
