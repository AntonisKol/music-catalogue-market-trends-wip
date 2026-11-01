"""ingest_label against faked Discogs and a real, throwaway PostgreSQL (see conftest.py)."""
import logging
from datetime import date

import psycopg
import pytest
import requests
import responses
from responses import matchers

from ingestion.discogs_client import BASE_URL
from ingestion.mapping import is_clearly_not_vinyl, is_vinyl
from ingestion.pipeline import DatabaseConnectionLost, ingest_label, unique_releases

DAY = date(2026, 11, 1)
VINYL = [{"name": "Vinyl", "qty": "1", "descriptions": ['12"']}]
CD = [{"name": "CD", "qty": "1"}]
STATS = {"num_for_sale": 3, "lowest_price": {"value": 6.5, "currency": "EUR"}, "blocked_from_sale": False}


def summary(release_id, format_text):
    return {"id": release_id, "title": f"Release {release_id}", "format": format_text}


def label_list(*summaries, label_id=1):
    responses.get(
        f"{BASE_URL}/labels/{label_id}/releases",
        json={"pagination": {"page": 1, "pages": 1}, "releases": list(summaries)},
    )


def details(release_id, formats=VINYL, status=200):
    release = {
        "id": release_id,
        "title": f"Release {release_id}",
        "formats": formats,
        "community": {"want": 10, "have": 20, "rating": {"count": 2, "average": 4.5}},
    }
    responses.get(f"{BASE_URL}/releases/{release_id}", json=release if status == 200 else {"message": "x"}, status=status)


def stats(release_id, body=STATS, status=200):
    responses.get(
        f"{BASE_URL}/marketplace/stats/{release_id}",
        match=[matchers.query_param_matcher({"curr_abbr": "EUR"})],
        json=body,
        status=status,
    )


def requested(path):
    return sum(1 for call in responses.calls if call.request.url.split("?")[0] == f"{BASE_URL}{path}")


@pytest.fixture
def committed(postgres_url):
    def query(sql, params=None):
        with psycopg.connect(postgres_url) as conn:
            return conn.execute(sql, params).fetchall()

    return query


@pytest.mark.parametrize(
    "text, expected",
    [
        ("CD, Comp", True),
        ("2xCD, Comp, Ltd", True),
        ("14xFile, AIFF, Album, 24-", True),
        ("Cass", True),
        ("VHS", True),
        ("CD, Mini, 3\"", True),
        ('12"', False),
        ("2xLP", False),
        ('CD + 12"', False),
        ('5x10", Album + Box', False),
        ("Box, Comp", False),
        ("", False),
        (None, False),
    ],
)
def test_is_clearly_not_vinyl(text, expected):
    assert is_clearly_not_vinyl(text) is expected


def test_is_vinyl():
    assert is_vinyl(VINYL)
    assert is_vinyl(CD + VINYL)
    assert not is_vinyl(CD)
    assert not is_vinyl([])


def test_unique_releases_keeps_the_first_of_each():
    assert [s["format"] for s in unique_releases([summary(1, "a"), summary(2, "b"), summary(1, "c")])] == ["a", "b"]


@responses.activate
def test_ingests_vinyl_skips_the_rest_and_reports_failures(db, committed, caplog):
    caplog.set_level(logging.INFO)
    label_list(
        summary(1, '12"'),
        summary(1, '12"'),  # listed twice by Discogs
        summary(2, "CD, Comp"),  # clearly not vinyl: no details fetched
        summary(3, 'CD + 12"'),  # combination: the details decide
        summary(4, "Box, Comp"),  # unclear: the details show a CD
        summary(5, '12"'),  # details fail
        summary(6, '7"'),
    )
    details(1)
    stats(1)
    details(3, formats=CD + VINYL)
    stats(3)
    details(4, formats=CD)
    details(5, status=404)
    details(6)
    stats(6)

    run = ingest_label(db, 1, DAY)

    assert run.listed == 6
    assert run.saved == [1, 3, 6]
    assert run.not_vinyl == [2, 4]
    assert list(run.failed) == [5]
    assert "404" in run.failed[5]
    assert not run.ok
    assert requested("/releases/1") == 1
    assert requested("/releases/2") == 0
    assert requested("/marketplace/stats/4") == 0
    assert committed("SELECT id FROM releases ORDER BY id") == [(1,), (3,), (6,)]
    assert committed("SELECT release_id, snapshot_date FROM price_snapshots ORDER BY 1") == [
        (1, DAY), (3, DAY), (6, DAY)
    ]
    assert "[6/6] 6 saved: 3 for sale from 6.50 EUR; want 10, have 20; rated 4.50 by 2" in caplog.text
    assert any(r.levelname == "ERROR" and "[5/6] 5 failed" in r.message for r in caplog.records)


@responses.activate
def test_a_temporary_discogs_error_is_retried_within_the_run(db, committed):
    label_list(summary(1, '12"'))
    details(1)
    stats(1, status=503)
    stats(1)

    run = ingest_label(db, 1, DAY)

    assert run.saved == [1] and run.ok
    assert requested("/marketplace/stats/1") == 2


@responses.activate
def test_every_release_is_committed_as_soon_as_it_is_saved(db, postgres_url):
    """A crash late in a run must not lose the releases saved before it."""
    label_list(summary(1, '12"'), summary(2, '12"'), summary(3, '12"'))
    details(1)
    stats(1)
    details(2)
    stats(2)
    seen_while_running = []

    def release_3(request):
        with psycopg.connect(postgres_url) as other:
            seen_while_running.extend(other.execute("SELECT id FROM releases ORDER BY id").fetchall())
        return 404, {}, '{"message": "Release not found."}'

    responses.add_callback(responses.GET, f"{BASE_URL}/releases/3", callback=release_3)

    ingest_label(db, 1, DAY)

    assert seen_while_running == [(1,), (2,)]


@responses.activate
def test_database_error_on_one_release_does_not_stop_the_others(db, committed):
    label_list(summary(1, '12"'), summary(2, '12"'))
    details(1)
    # A rating of 10 does not fit NUMERIC(3,2), so release 1's snapshot fails.
    responses.replace(
        responses.GET,
        f"{BASE_URL}/releases/1",
        json={"id": 1, "title": "Bad", "formats": VINYL, "community": {"want": 1, "have": 1, "rating": {"count": 1, "average": 10}}},
    )
    stats(1)
    details(2)
    stats(2)

    run = ingest_label(db, 1, DAY)

    assert list(run.failed) == [1]
    assert run.saved == [2]
    assert committed("SELECT release_id FROM price_snapshots") == [(2,)]


@responses.activate
def test_lost_database_connection_stops_the_run(db, postgres_url):
    label_list(summary(1, '12"'), summary(2, '12"'))
    stats(1)
    details(2)
    stats(2)
    pid = db.info.backend_pid

    def details_then_kill_connection(request):
        with psycopg.connect(postgres_url, autocommit=True) as admin:
            admin.execute("SELECT pg_terminate_backend(%s)", (pid,))
        return 200, {}, '{"id": 1, "title": "Release 1", "formats": [{"name": "Vinyl"}]}'

    responses.add_callback(responses.GET, f"{BASE_URL}/releases/1", callback=details_then_kill_connection)

    with pytest.raises(DatabaseConnectionLost):
        ingest_label(db, 1, DAY)
    assert requested("/releases/2") == 0


@responses.activate
def test_label_list_failure_is_raised(db):
    responses.get(f"{BASE_URL}/labels/1/releases", status=404)

    with pytest.raises(requests.HTTPError):
        ingest_label(db, 1, DAY)


@responses.activate
def test_running_twice_the_same_day_updates_instead_of_duplicating(db, committed):
    label_list(summary(1, '12"'))
    details(1)
    stats(1)

    ingest_label(db, 1, DAY)
    ingest_label(db, 1, DAY)

    assert committed("SELECT count(*) FROM releases") == [(1,)]
    assert committed("SELECT count(*) FROM price_snapshots") == [(1,)]
    assert committed("SELECT count(*) FROM demand_snapshots") == [(1,)]
