"""Accounts: usernames, passwords and sessions.

Nobody can register from the web: the site's owner creates accounts and resets
passwords with web/manage_accounts.py. Passwords are stored only as salted scrypt
hashes, and a session's random token only as a SHA-256 hash, so a copy of the
database cannot be used to sign in. Every time comes from the caller, so tests
can move the clock.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

import psycopg
from werkzeug.security import check_password_hash, generate_password_hash

SESSION_LIFETIME = timedelta(days=30)
MAX_USERNAME_LENGTH = 64
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

USERNAME = re.compile(r"[a-z0-9][a-z0-9._-]{2,63}")
# Checked when the username is unknown, so that a wrong username takes as long as
# a wrong password and the time taken does not reveal which usernames exist.
UNUSABLE_HASH = generate_password_hash(secrets.token_urlsafe(32))


class AccountExists(Exception):
    pass


def normalize_username(text: str) -> str:
    """The username as stored, lower-cased; ValueError if it cannot be one."""
    username = (text or "").strip().lower()
    if not USERNAME.fullmatch(username):
        raise ValueError("A username is 3 to 64 characters: letters, digits, dots, dashes and underscores.")
    return username


def check_new_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"A password is {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.")


def username_key(username: str, secret: str) -> str:
    """A keyed hash of a typed username, for counting attempts without storing what was typed."""
    return hmac.new(secret.encode(), username.strip().lower().encode(), hashlib.sha256).hexdigest()[:32]


def hashed(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_account(conn: psycopg.Connection, username: str, password: str, now: datetime) -> int:
    """Add an account and return its id; AccountExists if the username is taken."""
    username = normalize_username(username)
    check_new_password(password)
    with conn.transaction():
        row = conn.execute(
            "INSERT INTO accounts (username, password_hash, created_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (username) DO NOTHING RETURNING id",
            (username, generate_password_hash(password), now),
        ).fetchone()
    if row is None:
        raise AccountExists(username)
    return row[0]


def set_password(conn: psycopg.Connection, username: str, password: str) -> bool:
    """Change a password and end the account's sessions. False if there is no such account."""
    username = normalize_username(username)
    check_new_password(password)
    with conn.transaction():
        row = conn.execute("UPDATE accounts SET password_hash = %s WHERE username = %s RETURNING id",
                           (generate_password_hash(password), username)).fetchone()
        if row is None:
            return False
        conn.execute("DELETE FROM sessions WHERE account_id = %s", (row[0],))
    return True


def sign_in(conn: psycopg.Connection, username: str, password: str, now: datetime):
    """Check a username and password and start a session.

    Returns (session token, account id, username), or None for every kind of
    wrong sign-in, so a caller cannot tell an unknown username from a wrong password.
    """
    username = (username or "").strip().lower()
    with conn.transaction():
        row = conn.execute("SELECT id, password_hash FROM accounts WHERE username = %s", (username,)).fetchone()
    # Checked outside the transaction: the hash is slow on purpose.
    matches = check_password_hash(row[1] if row else UNUSABLE_HASH, password)
    if row is None or not matches:
        return None
    session = secrets.token_urlsafe(32)
    with conn.transaction():
        conn.execute("UPDATE accounts SET last_sign_in_at = %s WHERE id = %s", (now, row[0]))
        conn.execute("DELETE FROM sessions WHERE expires_at < %s", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (hashed(session), row[0], now, now + SESSION_LIFETIME),
        )
    return session, row[0], username


def session_account(conn: psycopg.Connection, session: str, now: datetime):
    """(account id, username) for a live session token, or None."""
    if not session:
        return None
    with conn.transaction():
        return conn.execute(
            "SELECT a.id, a.username FROM sessions s JOIN accounts a ON a.id = s.account_id"
            " WHERE s.token_hash = %s AND s.expires_at > %s",
            (hashed(session), now),
        ).fetchone()


def sign_out(conn: psycopg.Connection, session: str) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hashed(session),))


def delete_account(conn: psycopg.Connection, account_id: int, counter_key: str) -> None:
    """Delete the account, its sessions (by cascade) and its question counts."""
    with conn.transaction():
        conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        conn.execute("DELETE FROM question_counts WHERE visitor = %s", (counter_key,))


def find_account(conn: psycopg.Connection, username: str):
    """The id of an account, or None."""
    with conn.transaction():
        row = conn.execute("SELECT id FROM accounts WHERE username = %s", (username.strip().lower(),)).fetchone()
    return row[0] if row else None


def list_accounts(conn: psycopg.Connection) -> list:
    """(username, created_at, last_sign_in_at) of every account. No passwords."""
    with conn.transaction():
        return conn.execute("SELECT username, created_at, last_sign_in_at FROM accounts ORDER BY username").fetchall()
