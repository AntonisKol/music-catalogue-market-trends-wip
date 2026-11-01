"""The save_snapshot command: exit codes and messages, with Discogs faked."""
from datetime import datetime, timezone

import psycopg
import pytest
import responses
from responses import matchers

from ingestion import save_snapshot as command
from ingestion.discogs_client import BASE_URL

RELEASE = {
    "id": 249504,
    "title": "Never Gonna Give You Up",
    "community": {"have": 4159, "want": 594, "rating": {"count": 249, "average": 3.88}},
}
STATS = {"num_for_sale": 124, "lowest_price": {"value": 0.59, "currency": "EUR"}, "blocked_from_sale": False}


def fake_discogs(release=RELEASE, stats=STATS, stats_status=200):
    responses.get(f"{BASE_URL}/releases/249504", json=release)
    responses.get(
        f"{BASE_URL}/marketplace/stats/249504",
        match=[matchers.query_param_matcher({"curr_abbr": "EUR"})],
        json=stats,
        status=stats_status,
    )


class FixedNow(datetime):
    """23:30 on 1 November in UTC is already 2 November in Berlin: the UTC day must win."""

    @classmethod
    def now(cls, tz=None):
        return datetime(2026, 11, 1, 23, 30, tzinfo=timezone.utc).astimezone(tz)


@pytest.fixture
def connect_to(monkeypatch):
    def use(connect):
        monkeypatch.setattr(command.database, "connect", connect)

    return use


@pytest.mark.parametrize("argv", [[], ["abc"], ["1", "2"]])
def test_usage_error(argv, capsys):
    assert command.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


@responses.activate
def test_saves_and_prints_todays_snapshot(postgres_url, db, connect_to, monkeypatch, capsys):
    fake_discogs()
    connect_to(lambda: psycopg.connect(postgres_url))
    monkeypatch.setattr(command, "datetime", FixedNow)
    today = FixedNow.now(timezone.utc).date()

    assert command.main(["249504"]) == 0
    assert capsys.readouterr().out == (
        f"Snapshot of 249504 on {today} (UTC): 124 for sale from 0.59 EUR; "
        "want 594, have 4159; rated 3.88 by 249\n"
    )
    with psycopg.connect(postgres_url) as conn:
        assert conn.execute("SELECT snapshot_date FROM price_snapshots").fetchall() == [(today,)]
        assert conn.execute("SELECT title FROM releases").fetchall() == [("Never Gonna Give You Up",)]


@responses.activate
def test_prints_nothing_for_sale_and_not_rated(postgres_url, db, connect_to, capsys):
    fake_discogs(
        release=dict(RELEASE, community={"have": 1, "want": 0, "rating": {"count": 0, "average": 0.0}}),
        stats={"num_for_sale": 0, "lowest_price": None, "blocked_from_sale": False},
    )
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["249504"]) == 0
    assert capsys.readouterr().out.endswith(": 0 for sale; want 0, have 1; not rated\n")


@responses.activate
def test_discogs_stats_failure_saves_nothing(connect_to, capsys):
    fake_discogs(stats={"message": "error"}, stats_status=429)
    connect_to(lambda: pytest.fail("must not connect when Discogs failed"))

    assert command.main(["249504"]) == 1
    assert "Discogs request for release 249504 failed" in capsys.readouterr().err


@responses.activate
def test_database_unreachable(connect_to, capsys):
    fake_discogs()

    def refuse():
        raise psycopg.OperationalError("connection refused")

    connect_to(refuse)

    assert command.main(["249504"]) == 1
    assert "Saving the snapshot of release 249504 failed: connection refused" in capsys.readouterr().err


@responses.activate
def test_snapshot_failure_keeps_the_saved_release(postgres_url, db, connect_to, capsys):
    fake_discogs(release=dict(RELEASE, community={"have": 1, "want": 1, "rating": {"count": 1, "average": 10}}))
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["249504"]) == 1
    assert "Saving the snapshot of release 249504 failed" in capsys.readouterr().err
    with psycopg.connect(postgres_url) as conn:
        assert conn.execute("SELECT id FROM releases").fetchall() == [(249504,)]
        assert conn.execute("SELECT count(*) FROM price_snapshots").fetchall() == [(0,)]
