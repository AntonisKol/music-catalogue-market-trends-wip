"""save_snapshots against a real, throwaway PostgreSQL (see conftest.py)."""
from datetime import date
from decimal import Decimal

import psycopg
import pytest

from ingestion.repository import get_saved_snapshot, save_price_snapshot, save_release, save_snapshots

DAY = date(2026, 11, 1)
RELEASE = {
    "id": 249504,
    "title": "Never Gonna Give You Up",
    "community": {"have": 4159, "want": 594, "rating": {"count": 249, "average": 3.88}},
}
STATS = {"num_for_sale": 124, "lowest_price": {"value": 0.59, "currency": "EUR"}, "blocked_from_sale": False}


@pytest.fixture
def committed(postgres_url):
    """Run a query on a separate connection, so it only sees committed rows."""

    def query(sql, params=None):
        with psycopg.connect(postgres_url) as conn:
            return conn.execute(sql, params).fetchall()

    return query


@pytest.fixture
def saved_release(db):
    save_release(db, RELEASE)
    return RELEASE["id"]


def community(want, have, count, average):
    return dict(RELEASE, community={"want": want, "have": have, "rating": {"count": count, "average": average}})


def test_saves_price_and_demand_snapshot(db, saved_release, committed):
    save_snapshots(db, saved_release, STATS, RELEASE, DAY)

    assert committed(
        "SELECT release_id, snapshot_date, num_for_sale, lowest_price, currency FROM price_snapshots"
    ) == [(249504, DAY, 124, Decimal("0.59"), "EUR")]
    assert committed(
        "SELECT release_id, snapshot_date, want, have, rating_count, rating_average FROM demand_snapshots"
    ) == [(249504, DAY, 594, 4159, 249, Decimal("3.88"))]
    assert get_saved_snapshot(db, 249504, DAY) == (124, Decimal("0.59"), "EUR", 594, 4159, 249, Decimal("3.88"), DAY)
    assert get_saved_snapshot(db, 249504, date(2026, 11, 2)) is None


def test_same_day_again_replaces_that_days_values(db, saved_release, committed):
    save_snapshots(db, saved_release, STATS, RELEASE, DAY)
    save_snapshots(
        db,
        saved_release,
        {"num_for_sale": 120, "lowest_price": {"value": 0.65, "currency": "EUR"}},
        community(600, 4160, 250, 3.9),
        DAY,
    )

    assert committed("SELECT num_for_sale, lowest_price FROM price_snapshots") == [(120, Decimal("0.65"))]
    assert committed("SELECT want, have, rating_count, rating_average FROM demand_snapshots") == [
        (600, 4160, 250, Decimal("3.90"))
    ]


def test_each_day_gets_its_own_snapshot(db, saved_release, committed):
    save_snapshots(db, saved_release, STATS, RELEASE, DAY)
    save_snapshots(db, saved_release, STATS, RELEASE, date(2026, 11, 2))

    assert committed("SELECT snapshot_date FROM price_snapshots ORDER BY 1") == [(DAY,), (date(2026, 11, 2),)]
    assert committed("SELECT count(*) FROM demand_snapshots") == [(2,)]


def test_nothing_for_sale_and_no_ratings(db, saved_release, committed):
    save_snapshots(
        db, saved_release, {"num_for_sale": 0, "lowest_price": None}, community(0, 1, 0, 0.0), DAY
    )

    assert committed("SELECT num_for_sale, lowest_price, currency FROM price_snapshots") == [(0, None, None)]
    assert committed("SELECT want, have, rating_count, rating_average FROM demand_snapshots") == [
        (0, 1, 0, None)
    ]


def test_snapshot_of_a_release_not_in_the_catalogue_fails(db, committed):
    with pytest.raises(psycopg.errors.ForeignKeyViolation):
        save_snapshots(db, 249504, STATS, RELEASE, DAY)

    assert committed("SELECT count(*) FROM price_snapshots") == [(0,)]


def test_failed_snapshot_keeps_neither_row_and_leaves_the_catalogue(db, saved_release, committed):
    # The price row is written first; the demand row then fails (10 does not fit NUMERIC(3,2)).
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        save_snapshots(db, saved_release, STATS, community(1, 1, 1, 10), DAY)

    assert committed("SELECT count(*) FROM price_snapshots") == [(0,)]
    assert committed("SELECT count(*) FROM demand_snapshots") == [(0,)]
    assert committed("SELECT id, title FROM releases") == [(249504, "Never Gonna Give You Up")]


def test_failed_same_day_resave_keeps_the_earlier_snapshot(db, saved_release, committed):
    save_snapshots(db, saved_release, STATS, RELEASE, DAY)

    with pytest.raises(psycopg.Error):
        save_snapshots(
            db, saved_release, {"num_for_sale": 1, "lowest_price": {"value": 1, "currency": "EUR"}},
            community(1, 1, 1, 10), DAY,
        )

    assert committed("SELECT num_for_sale, lowest_price FROM price_snapshots") == [(124, Decimal("0.59"))]


def test_a_price_only_snapshot_is_read_with_the_latest_details_before_it(db, saved_release, committed):
    save_snapshots(db, saved_release, STATS, RELEASE, DAY)
    later = date(2026, 11, 3)
    save_price_snapshot(db, saved_release, {"num_for_sale": 7, "lowest_price": {"value": 2.5, "currency": "EUR"}}, later)

    assert committed("SELECT snapshot_date FROM price_snapshots ORDER BY 1") == [(DAY,), (later,)]
    assert committed("SELECT snapshot_date FROM demand_snapshots") == [(DAY,)]
    assert get_saved_snapshot(db, 249504, later) == (7, Decimal("2.50"), "EUR", 594, 4159, 249, Decimal("3.88"), DAY)


def test_details_saved_after_the_day_are_not_read_for_it(db, saved_release):
    save_price_snapshot(db, saved_release, STATS, DAY)
    save_snapshots(db, saved_release, STATS, RELEASE, date(2026, 11, 2))

    assert get_saved_snapshot(db, 249504, DAY) == (124, Decimal("0.59"), "EUR", None, None, None, None, None)
