"""Question limits: the counting login, and the counts per visitor and for the whole site."""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from ingestion import create_limits_role as command
from ingestion.create_limits_role import create_limits_role
from web.limits import LimitReached, connect_limits, take_question, visitor_id

NOON = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)


def take(conn, visitor="v1", now=NOON, per_hour=3, per_day=5, site=10):
    take_question(conn, visitor, now, per_hour, per_day, site)


def counts(db):
    db.commit()
    rows = db.execute("SELECT visitor, period, window_start, count FROM question_counts ORDER BY 1, 2, 3").fetchall()
    db.commit()
    return rows


@pytest.fixture
def limits(limits_db):
    with connect_limits() as conn:
        yield conn


def test_one_question_takes_one_from_each_count(limits, db):
    take(limits)

    hour, day = NOON.replace(minute=0), NOON.replace(hour=0, minute=0)
    assert counts(db) == [("site", "day", day, 1), ("v1", "day", day, 1), ("v1", "hour", hour, 1)]


def test_the_hourly_limit_refuses_without_counting(limits, db):
    for _ in range(3):
        take(limits)

    with pytest.raises(LimitReached) as reached:
        take(limits)

    assert reached.value.code == "visitor_limit_reached"
    assert reached.value.retry_after == 30 * 60 + 1  # until 13:00
    assert {(who, period): n for who, period, _, n in counts(db)} == {("site", "day"): 3, ("v1", "day"): 3, ("v1", "hour"): 3}


def test_a_new_hour_allows_more_until_the_daily_limit(limits):
    for hour in range(5):
        take(limits, now=NOON + timedelta(hours=hour))

    with pytest.raises(LimitReached) as reached:
        take(limits, now=NOON + timedelta(hours=5))
    assert "a day" in str(reached.value)
    assert reached.value.retry_after == int((datetime(2026, 11, 2, tzinfo=timezone.utc) - (NOON + timedelta(hours=5))).total_seconds()) + 1


def test_a_new_day_starts_from_zero(limits):
    for hour in range(5):
        take(limits, now=NOON + timedelta(hours=hour - 5))  # 07:30 to 11:30
    take(limits, now=NOON + timedelta(days=1))


def test_the_site_wide_cap_stops_every_visitor(limits):
    for n in range(10):
        take(limits, visitor=f"visitor-{n}")

    with pytest.raises(LimitReached) as reached:
        take(limits, visitor="a-new-visitor")
    assert reached.value.code == "site_limit_reached"


def test_visitors_are_counted_separately(limits):
    for _ in range(3):
        take(limits, visitor="a")
    take(limits, visitor="b")
    with pytest.raises(LimitReached):
        take(limits, visitor="a")


def test_counts_older_than_two_days_are_deleted(limits, db):
    take(limits, now=NOON - timedelta(days=3))
    take(limits, now=NOON)

    assert {window.date() for _, _, window, _ in counts(db)} == {NOON.date()}


def test_visitor_ids_are_keyed_hashes_not_addresses():
    ip = "203.0.113.7"
    one = visitor_id(ip, "secret-one")

    assert ip not in one and len(one) == 32
    assert one == visitor_id(ip, "secret-one")
    assert one != visitor_id(ip, "secret-two")
    assert one != visitor_id("203.0.113.8", "secret-one")


def test_the_limits_login_can_only_touch_its_table(limits_db, db):
    db.execute("INSERT INTO labels (id, name) VALUES (1, 'Planet E')")
    db.commit()
    with connect_limits() as conn:
        for statement in ["SELECT * FROM labels", "INSERT INTO labels (id, name) VALUES (2, 'x')",
                          "DELETE FROM releases", "CREATE TABLE stolen (id int)"]:
            with pytest.raises(psycopg.errors.InsufficientPrivilege):
                conn.execute(statement)
        conn.execute("SELECT count(*) FROM question_counts")


def test_rerunning_the_command_changes_the_password_and_keeps_the_counts(limits, postgres_url, limits_role, db):
    take(limits)
    with psycopg.connect(postgres_url) as owner:
        create_limits_role(owner, limits_role, "a-new-password")
    try:
        assert len(counts(db)) == 3
    finally:
        with psycopg.connect(postgres_url) as owner:
            create_limits_role(owner, limits_role, "limits-test-password")


def test_rerunning_adds_label_notification_status_to_an_existing_database(postgres_url, limits_role):
    with psycopg.connect(postgres_url, autocommit=True) as owner:
        owner.execute("ALTER TABLE label_requests DROP COLUMN notification_sent_at")

        create_limits_role(owner, limits_role, "limits-test-password")

        assert owner.execute(
            "SELECT column_name FROM information_schema.columns"
            " WHERE table_name = 'label_requests' AND column_name = 'notification_sent_at'"
        ).fetchone() == ("notification_sent_at",)


@pytest.mark.parametrize("name", ["Robert'); DROP TABLE labels;--", "UPPER", ""])
def test_unsafe_role_names_are_refused(name):
    with pytest.raises(ValueError):
        create_limits_role(None, name, "pw")


def test_command_needs_both_settings(monkeypatch, capsys):
    monkeypatch.delenv("POSTGRES_LIMITS_PASSWORD")

    assert command.main([]) == 1
    assert "POSTGRES_LIMITS_USER and POSTGRES_LIMITS_PASSWORD" in capsys.readouterr().err
