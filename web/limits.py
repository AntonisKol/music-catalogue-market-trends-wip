"""How often visitors, and the site as a whole, may ask.

Counts are kept in PostgreSQL (question_counts, see ingestion/create_limits_role.py)
per fixed UTC hour and day, so they survive restarts and are shared by every API
process. One question takes one unit from each of three counts at once: the
visitor's hour, the visitor's day and the site's day. If any count would go over
its limit, none is taken and the question is refused.
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


def take_question(conn: psycopg.Connection, visitor: str, now: datetime,
                  per_hour: int, per_day: int, site_per_day: int) -> None:
    """Count one question, or raise LimitReached without counting anything."""
    now = now.astimezone(timezone.utc)
    hour = now.replace(minute=0, second=0, microsecond=0)
    day = hour.replace(hour=0)
    checks = [
        (SITE, "day", day, site_per_day, "site_limit_reached",
         "The question box has reached today's limit. Please come back tomorrow.", day + timedelta(days=1)),
        (visitor, "hour", hour, per_hour, "visitor_limit_reached",
         f"You can ask {per_hour} questions an hour. Please try again later.", hour + timedelta(hours=1)),
        (visitor, "day", day, per_day, "visitor_limit_reached",
         f"You can ask {per_day} questions a day. Please come back tomorrow.", day + timedelta(days=1)),
    ]
    with conn.transaction():
        conn.execute("DELETE FROM question_counts WHERE window_start < %s", (now - KEEP,))
        for who, period, window_start, limit, code, message, ends in checks:
            count = conn.execute(TAKE, {"visitor": who, "period": period, "window_start": window_start}).fetchone()[0]
            if count > limit:
                # Raising inside the transaction undoes every count taken above.
                raise LimitReached(code, message, seconds_until(ends, now))
