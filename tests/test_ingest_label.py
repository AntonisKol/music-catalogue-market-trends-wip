"""The ingest_label command: exit codes and the logged summary, with Discogs faked."""
import logging

import psycopg
import pytest
import responses

from ingestion import ingest_label as command
from ingestion.discogs_client import BASE_URL
from ingestion.pipeline import DatabaseConnectionLost

VINYL = [{"name": "Vinyl"}]


def fake_label(release_ids, fail=()):
    responses.get(
        f"{BASE_URL}/labels/1/releases",
        json={"pagination": {"page": 1, "pages": 1}, "releases": [{"id": i, "format": '12"'} for i in release_ids]},
    )
    for i in release_ids:
        if i in fail:
            responses.get(f"{BASE_URL}/releases/{i}", json={"message": "Release not found."}, status=404)
            continue
        responses.get(f"{BASE_URL}/releases/{i}", json={"id": i, "title": f"Release {i}", "formats": VINYL})
        responses.get(f"{BASE_URL}/marketplace/stats/{i}", json={"num_for_sale": 0, "lowest_price": None})


@pytest.fixture
def connect_to(monkeypatch):
    def use(connect):
        monkeypatch.setattr(command.database, "connect", connect)

    return use


@pytest.fixture
def logs(caplog):
    caplog.set_level(logging.INFO)
    return caplog


@pytest.mark.parametrize("argv", [[], ["abc"], ["1", "2"]])
def test_usage_error(argv, capsys):
    assert command.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


@responses.activate
def test_all_saved_exits_0(postgres_url, db, connect_to, logs):
    fake_label([1, 2])
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["1"]) == 0
    assert "Label 1 done: 2 listed, 2 saved, 0 not vinyl, 0 failed" in logs.text


@responses.activate
def test_failed_releases_are_listed_and_exit_1(postgres_url, db, connect_to, logs):
    fake_label([1, 2, 3], fail={2})
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["1"]) == 1
    assert "Label 1 done: 3 listed, 2 saved, 0 not vinyl, 1 failed" in logs.text
    assert any(
        r.levelname == "ERROR" and r.message.startswith("Failed release 2: 404") for r in logs.records
    )


@responses.activate
def test_label_list_failure_exits_1(postgres_url, db, connect_to, logs):
    responses.get(f"{BASE_URL}/labels/1/releases", json={"message": "not found"}, status=404)
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["1"]) == 1
    assert "Discogs request for label 1 failed" in logs.text


def test_database_unreachable_exits_1(connect_to, logs):
    def refuse():
        raise psycopg.OperationalError("connection refused")

    connect_to(refuse)

    assert command.main(["1"]) == 1
    assert "Could not connect to PostgreSQL: connection refused" in logs.text


def test_lost_connection_exits_1(connect_to, monkeypatch, logs):
    class Conn:
        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    def lost(conn, label_id, snapshot_date):
        raise DatabaseConnectionLost("server closed the connection")

    connect_to(Conn)
    monkeypatch.setattr(command, "ingest_label", lost)

    assert command.main(["1"]) == 1
    assert "Stopped: the database connection was lost" in logs.text
