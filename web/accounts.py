"""Accounts: signing up with an email address and a password, and sessions.

A visitor signs up with an address and a password. The account can't be used until
the visitor presses the button behind a link sent to that address, so an address
nobody can open never gets a working account. Unconfirmed accounts are deleted after
7 days. A forgotten password is replaced the same way, with a link.

Opening a link only shows a page; only pressing its button uses the link (mail link
scanners open links but never press buttons). Passwords are stored only as salted
scrypt hashes, and the random tokens in links and sessions only as SHA-256 hashes, so
a copy of the database cannot be used to sign in. Every time comes from the caller,
so tests can move the clock.
"""
import hashlib
import hmac
import re
import secrets
from datetime import datetime, timedelta

import psycopg
from werkzeug.security import check_password_hash, generate_password_hash

CONFIRM = "confirm"
RESET = "reset"
LINK_LIFETIME = {CONFIRM: timedelta(hours=24), RESET: timedelta(hours=1)}
UNCONFIRMED_LIFETIME = timedelta(days=7)
SESSION_LIFETIME = timedelta(days=30)
MAX_EMAIL_LENGTH = 254
MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 256

# Deliberately loose: one @, something before it, a dot in the domain, no spaces.
# The real check is that the person can open the link sent to the address.
EMAIL = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
# Checked when the address is unknown, so that an unknown address takes as long as
# a wrong password and the time taken does not reveal who has an account.
UNUSABLE_HASH = generate_password_hash(secrets.token_urlsafe(32))


class NotConfirmed(Exception):
    """The password is right, but the address has not been confirmed yet."""


def normalize_email(text: str) -> str:
    """The address as stored, lower-cased; ValueError if it cannot be one."""
    email = (text or "").strip().lower()
    if len(email) > MAX_EMAIL_LENGTH or not EMAIL.fullmatch(email):
        raise ValueError("not an email address")
    return email


def check_new_password(password: str) -> None:
    if not MIN_PASSWORD_LENGTH <= len(password) <= MAX_PASSWORD_LENGTH:
        raise ValueError(f"A password is {MIN_PASSWORD_LENGTH} to {MAX_PASSWORD_LENGTH} characters.")


def email_key(email: str, secret: str) -> str:
    """A keyed hash of an address, for counting requests without storing it."""
    return hmac.new(secret.encode(), email.encode(), hashlib.sha256).hexdigest()[:32]


def hashed(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def new_token() -> str:
    return secrets.token_urlsafe(32)


def _new_link(conn: psycopg.Connection, account_id: int, purpose: str, now: datetime) -> str:
    """Replace the account's links of this kind with a new one, and return its token."""
    token = new_token()
    conn.execute("DELETE FROM email_tokens WHERE account_id = %s AND purpose = %s", (account_id, purpose))
    conn.execute(
        "INSERT INTO email_tokens (token_hash, account_id, purpose, created_at, expires_at)"
        " VALUES (%s, %s, %s, %s, %s)",
        (hashed(token), account_id, purpose, now, now + LINK_LIFETIME[purpose]),
    )
    return token


def _start_session(conn: psycopg.Connection, account_id: int, now: datetime) -> str:
    session = new_token()
    conn.execute("UPDATE accounts SET last_sign_in_at = %s WHERE id = %s", (now, account_id))
    conn.execute("DELETE FROM sessions WHERE expires_at < %s", (now,))
    conn.execute(
        "INSERT INTO sessions (token_hash, account_id, created_at, expires_at) VALUES (%s, %s, %s, %s)",
        (hashed(session), account_id, now, now + SESSION_LIFETIME),
    )
    return session


def sign_up(conn: psycopg.Connection, email: str, password: str, now: datetime):
    """Store a new unconfirmed account and return the token for its confirmation link.

    Signing up again before confirming replaces the password and the link, so someone
    who got there first with another person's address can't keep their password once
    that person signs up. Returns None if the address already has a confirmed account,
    which is left untouched.
    """
    # Before the transaction, and for every address: the hash is slow on purpose.
    password_hash = generate_password_hash(password)
    with conn.transaction():
        conn.execute("DELETE FROM accounts WHERE confirmed_at IS NULL AND created_at < %s",
                     (now - UNCONFIRMED_LIFETIME,))
        conn.execute("DELETE FROM email_tokens WHERE expires_at < %s", (now - timedelta(days=1),))
        row = conn.execute(
            "INSERT INTO accounts (email, password_hash, created_at) VALUES (%s, %s, %s)"
            " ON CONFLICT (email) DO UPDATE SET password_hash = EXCLUDED.password_hash, created_at = EXCLUDED.created_at"
            " WHERE accounts.confirmed_at IS NULL RETURNING id",
            (email, password_hash, now),
        ).fetchone()
        return _new_link(conn, row[0], CONFIRM, now) if row else None


def link_email(conn: psycopg.Connection, token: str, purpose: str, now: datetime):
    """The address a link is for, if it can still be used; it is not used up."""
    with conn.transaction():
        row = conn.execute(
            "SELECT a.email FROM email_tokens t JOIN accounts a ON a.id = t.account_id"
            " WHERE t.token_hash = %s AND t.purpose = %s AND t.used_at IS NULL AND t.expires_at > %s",
            (hashed(token), purpose, now),
        ).fetchone()
    return row[0] if row else None


def _use_link(conn: psycopg.Connection, token: str, purpose: str, now: datetime):
    """Mark a link used and return its account's id, or None. Checked and marked in one
    statement, so two presses of the button (or two tabs) cannot both use one link."""
    row = conn.execute(
        "UPDATE email_tokens SET used_at = %(now)s WHERE token_hash = %(hash)s AND purpose = %(purpose)s"
        " AND used_at IS NULL AND expires_at > %(now)s RETURNING account_id",
        {"hash": hashed(token), "purpose": purpose, "now": now},
    ).fetchone()
    return row[0] if row else None


def confirm_email(conn: psycopg.Connection, token: str, now: datetime):
    """Use a confirmation link: the account can now be used. (session token, account id), or None."""
    with conn.transaction():
        account_id = _use_link(conn, token, CONFIRM, now)
        if account_id is None:
            return None
        conn.execute("UPDATE accounts SET confirmed_at = %s WHERE id = %s", (now, account_id))
        return _start_session(conn, account_id, now), account_id


def sign_in(conn: psycopg.Connection, email: str, password: str, now: datetime):
    """Check an address and password and start a session: (session token, account id).

    None for an unknown address or a wrong password alike. NotConfirmed only for
    someone who knows the password.
    """
    with conn.transaction():
        row = conn.execute("SELECT id, password_hash, confirmed_at FROM accounts WHERE email = %s", (email,)).fetchone()
    # Checked outside the transaction: the hash is slow on purpose.
    matches = check_password_hash(row[1] if row else UNUSABLE_HASH, password)
    if row is None or not matches:
        return None
    if row[2] is None:
        raise NotConfirmed(email)
    with conn.transaction():
        return _start_session(conn, row[0], now), row[0]


def ask_for_reset(conn: psycopg.Connection, email: str, now: datetime):
    """The token for a new-password link, or None unless the address has a confirmed account."""
    with conn.transaction():
        row = conn.execute("SELECT id FROM accounts WHERE email = %s AND confirmed_at IS NOT NULL", (email,)).fetchone()
        return _new_link(conn, row[0], RESET, now) if row else None


def reset_password(conn: psycopg.Connection, token: str, password: str, now: datetime):
    """Use a new-password link. Every other session ends. (session token, account id), or None."""
    password_hash = generate_password_hash(password)
    with conn.transaction():
        account_id = _use_link(conn, token, RESET, now)
        if account_id is None:
            return None
        conn.execute("UPDATE accounts SET password_hash = %s WHERE id = %s", (password_hash, account_id))
        conn.execute("DELETE FROM sessions WHERE account_id = %s", (account_id,))
        return _start_session(conn, account_id, now), account_id


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
    """Delete the account, its sessions and links (by cascade) and its question counts."""
    with conn.transaction():
        conn.execute("DELETE FROM accounts WHERE id = %s", (account_id,))
        conn.execute("DELETE FROM question_counts WHERE visitor = %s", (counter_key,))
