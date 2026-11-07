"""Email sign-in: accounts, one-time sign-in links and sessions.

A visitor asks for a link with their email address. The link holds a random
token that works once, within 15 minutes; opening it shows a confirm page, and
only pressing its button uses the token (mail link scanners open links but never
press buttons). Using it creates the account if needed and starts a session,
whose random token goes into a cookie. The database stores only SHA-256 hashes
of tokens, so a copy of it cannot be used to sign in. Every time comes from the
caller, so tests can move the clock.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

import psycopg

LINK_LIFETIME = timedelta(minutes=15)
SESSION_LIFETIME = timedelta(days=30)
MAX_EMAIL_LENGTH = 254

# Deliberately loose: one @, something before it, a dot in the domain, no spaces.
# The real check is that the person can open the link sent to the address.
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")


def normalize_email(text: str) -> str:
    """The address as stored, lower-cased; ValueError if it cannot be one."""
    email = (text or "").strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL.fullmatch(email):
        raise ValueError("not an email address")
    return email


def email_key(email: str, secret: str) -> str:
    """A keyed hash of an address, for counting sign-in requests without storing it."""
    return hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()[:32]


def hashed(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def create_sign_in_link(conn: psycopg.Connection, email: str, now: datetime) -> str:
    """Store a new one-time token for this address and return it."""
    token = new_token()
    with conn.transaction():
        conn.execute("DELETE FROM sign_in_tokens WHERE expires_at < %s", (now - timedelta(days=1),))
        conn.execute(
            "INSERT INTO sign_in_tokens (token_hash, email, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (hashed(token), email, now, now + LINK_LIFETIME),
        )
    return token


def link_email(conn: psycopg.Connection, token: str, now: datetime):
    """The address a token is for, if it can still be used; it is not used up."""
    with conn.transaction():
        row = conn.execute(
            "SELECT email FROM sign_in_tokens WHERE token_hash = %s AND used_at IS NULL AND expires_at > %s",
            (hashed(token), now),
        ).fetchone()
    return row[0] if row else None


def sign_in(conn: psycopg.Connection, token: str, now: datetime):
    """Use up a token and start a session: (session token, account id, email), or None.

    The token is marked used in the same statement that checks it, so two
    presses of the button (or two tabs) cannot both sign in with one link.
    """
    session = new_token()
    with conn.transaction():
        row = conn.execute(
            "UPDATE sign_in_tokens SET used_at = %(now)s"
            " WHERE token_hash = %(hash)s AND used_at IS NULL AND expires_at > %(now)s RETURNING email",
            {"hash": hashed(token), "now": now},
        ).fetchone()
        if row is None:
            return None
        email = row[0]
        account_id = conn.execute(
            "INSERT INTO accounts (email, created_at, last_sign_in_at) VALUES (%(email)s, %(now)s, %(now)s)"
            " ON CONFLICT (email) DO UPDATE SET last_sign_in_at = %(now)s RETURNING id",
            {"email": email, "now": now},
        ).fetchone()[0]
        conn.execute("DELETE FROM sessions WHERE expires_at < %s", (now,))
        conn.execute(
            "INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
            (hashed(session), account_id, now, now + SESSION_LIFETIME),
        )
    return session, account_id, email


def session_account(conn: psycopg.Connection, session: str, now: datetime):
    """(account id, email) for a live session token, or None."""
    if not session:
        return None
    with conn.transaction():
        return conn.execute(
            "SELECT a.id, a.email FROM sessions s JOIN accounts a ON a.id = s.account_id"
            " WHERE s.token_hash = %s AND s.expires_at > %s",
            (hashed(session), now),
        ).fetchone()


def sign_out(conn: psycopg.Connection, session: str) -> None:
    with conn.transaction():
        conn.execute("DELETE FROM sessions WHERE token_hash = %s", (hashed(session),))


def delete_account(conn: psycopg.Connection, account_id: int, counter_key: str) -> None:
    """Delete the account, its sessions, its unused links and its question counts."""
    with conn.transaction():
        email = conn.execute("DELETE FROM accounts WHERE id = %s RETURNING email", (account_id,)).fetchone()
        if email:
            conn.execute("DELETE FROM sign_in_tokens WHERE email = %s", (email[0],))
        conn.execute("DELETE FROM question_counts WHERE visitor = %s", (counter_key,))
