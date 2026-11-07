"""Create (or update) the web API's tables and the login it uses.

    python -m ingestion.create_limits_role

Run it once per database, as the app's own login (POSTGRES_USER), like
create_reader_role. The login's name and password come from POSTGRES_LIMITS_USER
and POSTGRES_LIMITS_PASSWORD. Running it again updates the password.

The web API writes two kinds of things: how many questions each visitor, account
and the whole site have asked in the current hour and day, and sign-in (accounts
and sessions). This login may read and change those three tables and nothing else, so the reader login stays read-only and the
catalogue cannot be changed from the web.
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
CREATE_TABLES = """
    CREATE TABLE IF NOT EXISTS question_counts (
        visitor TEXT NOT NULL,
        period TEXT NOT NULL CHECK (period IN ('hour', 'day')),
        window_start TIMESTAMPTZ NOT NULL,
        count INT NOT NULL CHECK (count >= 0),
        PRIMARY KEY (visitor, period, window_start)
    );
    -- Email sign-in is gone. Its accounts had no passwords, so they could not be
    -- used any more; they are removed with the email addresses they held.
    DROP TABLE IF EXISTS sign_in_tokens;
    DO $$
    BEGIN
        IF EXISTS (SELECT 1 FROM information_schema.columns WHERE table_schema = current_schema()
                   AND table_name = 'accounts' AND column_name = 'email') THEN
            DELETE FROM question_counts WHERE visitor LIKE 'account:%' OR visitor LIKE 'sign-in-%';
            DROP TABLE IF EXISTS sessions;
            DROP TABLE accounts;
        END IF;
    END $$;
    -- Created only with web/manage_accounts.py. Passwords are stored as salted scrypt hashes.
    CREATE TABLE IF NOT EXISTS accounts (
        id BIGSERIAL PRIMARY KEY,
        username TEXT NOT NULL UNIQUE,
        password_hash TEXT NOT NULL,
        created_at TIMESTAMPTZ NOT NULL,
        last_sign_in_at TIMESTAMPTZ
    );
    -- Sessions are stored only as SHA-256 hashes of their random tokens, so a copy
    -- of the database cannot be used to sign in.
    CREATE TABLE IF NOT EXISTS sessions (
        token_hash TEXT PRIMARY KEY,
        account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
        created_at TIMESTAMPTZ NOT NULL,
        expires_at TIMESTAMPTZ NOT NULL
    );
"""
TABLES = ("question_counts", "accounts", "sessions")


def create_limits_role(conn: psycopg.Connection, name: str, password: str) -> None:
    if not re.fullmatch(r"[a-z_][a-z0-9_]{0,62}", name):
        raise ValueError(f"Limits role name {name!r} must be lower-case letters, digits and _")
    if not password:
        raise ValueError("Limits role password must not be empty")
    role = sql.Identifier(name)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(CREATE_TABLES)
        exists = cur.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (name,)).fetchone()
        verb = "ALTER ROLE {} WITH LOGIN PASSWORD {}" if exists else "CREATE ROLE {} WITH LOGIN PASSWORD {}"
        cur.execute(sql.SQL(verb).format(role, sql.Literal(password)))
        cur.execute(sql.SQL("ALTER ROLE {} SET statement_timeout = {}").format(role, sql.Literal(STATEMENT_TIMEOUT)))
        db = cur.execute("SELECT current_database()").fetchone()[0]
        cur.execute(sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(sql.Identifier(db), role))
        cur.execute(sql.SQL("GRANT USAGE ON SCHEMA public TO {}").format(role))
        cur.execute(sql.SQL("REVOKE CREATE ON SCHEMA public FROM {}").format(role))
        for table in TABLES:
            cur.execute(sql.SQL("GRANT SELECT, INSERT, UPDATE, DELETE ON {} TO {}").format(sql.Identifier(table), role))
        cur.execute(sql.SQL("GRANT USAGE ON SEQUENCE accounts_id_seq TO {}").format(role))


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
    print(f"Web role {name} is ready: it may only read and change {', '.join(TABLES)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
