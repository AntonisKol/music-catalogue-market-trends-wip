"""Labels that signed-in visitors ask the site to track.

A request is only a name as the visitor typed it. Nothing is tracked until the
owner looks the name up on Discogs and approves the right label with
ingestion/label_requests.py, so the web side never calls Discogs and never needs
its token. Requests are deleted with the account.
"""
import re
from datetime import datetime

import psycopg

from web import limits

MIN_NAME_LENGTH = 2
MAX_NAME_LENGTH = 100
PER_ACCOUNT_DAY = 3
PER_SITE_DAY = 50
# Discogs numbers labels that share a name ("Wax (4)"), and many names end in "Records".
_DISCOGS_NUMBER = re.compile(r"\s*\(\d+\)$")
_SUFFIX = re.compile(r"\s+(records|recordings|music|schallplatten)$")


def clean_name(text: str) -> str:
    """The name with its spaces tidied; ValueError if it is too short or too long."""
    name = " ".join((text or "").split())
    if not MIN_NAME_LENGTH <= len(name) <= MAX_NAME_LENGTH:
        raise ValueError(f"Please type a label name of {MIN_NAME_LENGTH} to {MAX_NAME_LENGTH} characters.")
    return name


def comparable(name: str) -> str:
    """"Planet Rhythm Records", "planet rhythm" and "Planet Rhythm (3)" compare equal."""
    plain = _DISCOGS_NUMBER.sub("", name.strip()).casefold()
    return _SUFFIX.sub("", plain)


def tracked_match(name: str, tracked: list):
    """The tracked label this name means, if any."""
    wanted = comparable(name)
    return next((label for label in tracked if comparable(label) == wanted), None)


def request_label(conn: psycopg.Connection, account_id: int, name: str, now: datetime) -> tuple:
    """Store or find an equivalent request, atomically with its quota charge.

    Returns (request id, stored name, whether it was created, whether it was already notified).
    """
    with conn.transaction():
        conn.execute("SELECT id FROM accounts WHERE id = %s FOR UPDATE", (account_id,)).fetchone()
        existing = conn.execute(
            "SELECT id, name, notification_sent_at, status FROM label_requests WHERE account_id = %s ORDER BY id",
            (account_id,),
        ).fetchall()
        for request_id, existing_name, sent_at, status in existing:
            if comparable(existing_name) == comparable(name):
                return request_id, existing_name, False, status != "waiting" or sent_at is not None
        limits.take(conn, now, [
            (f"label-request:account:{account_id}", "day", PER_ACCOUNT_DAY, "request_limit_reached",
             f"You can request {PER_ACCOUNT_DAY} labels a day. Please try again tomorrow."),
            ("label-request:site", "day", PER_SITE_DAY, "request_limit_reached",
             "The site has had a lot of label requests today. Please try again tomorrow."),
        ])
        request_id = conn.execute(
            "INSERT INTO label_requests (account_id, name, created_at) VALUES (%s, %s, %s) RETURNING id",
            (account_id, name, now),
        ).fetchone()[0]
        return request_id, name, True, False


def mark_notification_sent(conn: psycopg.Connection, request_id: int, now: datetime) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE label_requests SET notification_sent_at = %s"
            " WHERE id = %s AND notification_sent_at IS NULL",
            (now, request_id),
        )


def requests_of(conn: psycopg.Connection, account_id: int) -> list:
    """This account's requests, newest first: [{"name", "status"}]."""
    with conn.transaction():
        rows = conn.execute("SELECT name, status FROM label_requests WHERE account_id = %s ORDER BY created_at DESC, id DESC",
                            (account_id,)).fetchall()
    return [{"name": name, "status": status} for name, status in rows]
