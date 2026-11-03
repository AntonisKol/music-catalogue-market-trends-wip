"""How often visitors, and the site as a whole, may ask.

Counts are kept in PostgreSQL (question_counts, see ingestion/create_limits_role.py)
per fixed UTC hour and day, so they survive restarts and are shared by every API
process. One question takes one unit from each of three counts at once: the
visitor's (or, when signed in, the account's) hour and day, and the site's day.
If any count would go over its limit, none is taken and the question is refused.
Requests for sign-in links are counted the same way, per email and per visitor.
"""
import hashlib
import hmac
from datetime import datetime, timedelta, timezone

import psycopg

from web import config

SITE = "site"
KEEP = timedelta(days=2)

TAKE = """
    INSERT INTO question_counts (visitor, period, window_start, count)
    VALUES (%(visitor)s, %(period)s, %(window_start)s, 1)
    ON CONFLICT (visitor, period, window_start) DO UPDATE SET count = question_counts.count + 1
    RETURNING count
"""


class LimitReached(Exception):
    def __init__(self, code: str, message: str, retry_after: int):
        super().__init__(message)
        self.code = code
        self.retry_after = retry_after


def connect_limits() -> psycopg.Connection:
    return psycopg.connect(
        host=config.POSTGRES_HOST,
        port=config.POSTGRES_PORT,
        user=config.POSTGRES_LIMITS_USER,
        password=config.POSTGRES_LIMITS_PASSWORD,
        dbname=config.POSTGRES_DB,
        options="-c statement_timeout=5s",
        autocommit=True,
    )


def visitor_id(ip_address: str, secret: str) -> str:
    """A keyed one-way hash: the same visitor always maps to the same id, but the
    address cannot be recovered from it without the secret."""
    return hmac.new(secret.encode(), ip_address.encode(), hashlib.sha256).hexdigest()[:32]


def seconds_until(end: datetime, now: datetime) -> int:
    return max(1, int((end - now).total_seconds()) + 1)


def windows(now: datetime) -> tuple:
    now = now.astimezone(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    return now, hour, hour.replace(hour=0)


def take(conn: psycopg.Connection, now: datetime, checks: list) -> dict:
    """Take one unit from each count, or raise LimitReached without taking any.

    checks: (who, "hour" or "day", limit, code, message). Returns what is left of
    each count, keyed by (who, period).
    """
    now, hour, day = windows(now)
    starts = {"hour": (hour, hour + timedelta(hours=1)), "day": (day, day + timedelta(days=1))}
    left = {}
    with conn.transaction():
        conn.execute("DELETE FROM question_counts WHERE window_start < %s", (now - KEEP,))
        for who, period, limit, code, message in checks:
            window_start, ends = starts[period]
            count = conn.execute(TAKE, {"visitor": who, "period": period, "window_start": window_start}).fetchone()[0]
            if count > limit:
                # Raising inside the transaction undoes every count taken above.
                raise LimitReached(code, message, seconds_until(ends, now))
            left[(who, period)] = limit - count
    return left


def take_question(conn: psycopg.Connection, who: str, now: datetime,
                  per_hour: int, per_day: int, site_per_day: int, signed_in: bool = False) -> dict:
    """Count one question for a visitor or account, and for the site, or raise
    LimitReached without counting anything. Returns {"hour": left, "day": left}."""
    code = "account_limit_reached" if signed_in else "visitor_limit_reached"
    later = "" if signed_in else " Sign in with your email to ask more, or"
    left = take(conn, now, [
        (SITE, "day", site_per_day, "site_limit_reached",
         "The question box has reached today's limit. Please come back tomorrow."),
        # The day before the hour: when both are used up, the longer wait is the true one.
        (who, "day", per_day, code,
         f"You can ask {per_day} questions a day.{later or ' Please'} come back tomorrow."),
        (who, "hour", per_hour, code,
         f"You can ask {per_hour} questions an hour.{later or ' Please'} try again later."),
    ])
    return {"hour": left[(who, "hour")], "day": left[(who, "day")]}


def remaining(conn: psycopg.Connection, who: str, now: datetime, per_hour: int, per_day: int) -> dict:
    """What is left of a visitor's or account's counts, without taking anything."""
    _, hour, day = windows(now)
    with conn.transaction():
        rows = dict(conn.execute(
            "SELECT period, count FROM question_counts WHERE visitor = %s"
            " AND ((period = 'hour' AND window_start = %s) OR (period = 'day' AND window_start = %s))",
            (who, hour, day),
        ).fetchall())
    return {"hour": max(0, per_hour - rows.get("hour", 0)), "day": max(0, per_day - rows.get("day", 0))}


# Sign-in links a visitor may ask for, per email address and per visitor.
SIGN_IN_PER_EMAIL = {"hour": 3, "day": 5}
SIGN_IN_PER_VISITOR = {"hour": 5, "day": 10}


def take_sign_in(conn: psycopg.Connection, email_key: str, visitor: str, now: datetime) -> None:
    """Count one sign-in link request, or raise LimitReached without counting."""
    message = "Too many sign-in links have been asked for. Please check your inbox, or try again later."
    take(conn, now, [
        *((f"sign-in-email:{email_key}", period, limit, "sign_in_limit_reached", message)
          for period, limit in SIGN_IN_PER_EMAIL.items()),
        *((f"sign-in-visitor:{visitor}", period, limit, "sign_in_limit_reached", message)
          for period, limit in SIGN_IN_PER_VISITOR.items()),
    ])
