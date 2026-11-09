"""The ingest_label command: exit codes and the logged summary, with Discogs faked."""
import logging

import psycopg
import pytest
import responses

from ingestion import ingest_label as command
from ingestion.discogs_client import BASE_URL
from ingestion.pipeline import DatabaseConnectionLost

VINYL = [{"name": "Vinyl"}]


def fake_label(release_ids, fail=(), gone=()):
    responses.get(
        f"{BASE_URL}/labels/1/releases",
        json={"pagination": {"page": 1, "pages": 1}, "releases": [{"id": i, "format": '12"'} for i in release_ids]},
    )
    for i in release_ids:
        if i in fail:
            responses.get(f"{BASE_URL}/releases/{i}", json={"message": "Unauthorized."}, status=401)
            continue
        if i in gone:
            responses.get(f"{BASE_URL}/releases/{i}", json={"message": "Release not found."}, status=404)
            continue
        responses.get(
            f"{BASE_URL}/releases/{i}",
            json={"id": i, "title": f"Release {i}", "labels": [{"id": 1, "name": "Label 1"}], "formats": VINYL},
        )
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
    assert "Label 1 done: 2 listed, 2 saved, 0 not vinyl, 0 not on this label, 0 gone, 0 failed" in logs.text


@responses.activate
def test_failed_releases_are_listed_and_exit_1(postgres_url, db, connect_to, logs):
    fake_label([1, 2, 3], fail={2})
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["1"]) == 1
    assert "Label 1 done: 3 listed, 2 saved, 0 not vinyl, 0 not on this label, 0 gone, 1 failed" in logs.text
    assert any(
        r.levelname == "ERROR" and r.message.startswith("Failed release 2: 401") for r in logs.records
    )


@responses.activate
def test_gone_releases_are_listed_but_do_not_fail_the_run(postgres_url, db, connect_to, logs):
    fake_label([1, 2], gone={2})
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["1"]) == 0
    assert "Label 1 done: 2 listed, 1 saved, 0 not vinyl, 0 not on this label, 1 gone, 0 failed" in logs.text
    assert any(r.levelname == "WARNING" and r.message == "Gone from Discogs: release 2" for r in logs.records)


@responses.activate
def test_a_second_run_does_not_start_while_one_is_in_progress(postgres_url, db, connect_to, logs):
    fake_label([1])
    connect_to(lambda: psycopg.connect(postgres_url))

    with psycopg.connect(postgres_url, autocommit=True) as first_run:
        first_run.execute("SELECT pg_advisory_lock(%s)", (command.RUN_LOCK_ID,))
        assert command.main(["1"]) == 1
        assert "Another ingestion run is in progress" in logs.text
        assert len(responses.calls) == 0

    # Once the first run's connection has closed, a new run starts normally.
    assert command.main(["1"]) == 0


@responses.activate
def test_with_wait_a_second_run_starts_when_the_first_finishes(postgres_url, db, connect_to, logs, monkeypatch):
    fake_label([1])
    connect_to(lambda: psycopg.connect(postgres_url))
    first_run = psycopg.connect(postgres_url, autocommit=True)
    first_run.execute("SELECT pg_advisory_lock(%s)", (command.RUN_LOCK_ID,))
    checks = []

    def first_run_finishes(seconds):
        # The waiting run sleeps between checks; the first run ends during the second sleep.
        checks.append(seconds)
        if len(checks) == 2:
            first_run.close()

    monkeypatch.setattr(command.time, "sleep", first_run_finishes)
    try:
        assert command.main(["--wait", "1"]) == 0
    finally:
        first_run.close()
    assert checks == [command.WAIT_SECONDS, command.WAIT_SECONDS]
    assert "waiting for it to finish" in logs.text
    assert "Label 1 done: 1 listed, 1 saved" in logs.text


@responses.activate
def test_with_wait_a_free_lock_starts_at_once(postgres_url, db, connect_to, logs, monkeypatch):
    fake_label([1])
    connect_to(lambda: psycopg.connect(postgres_url))
    monkeypatch.setattr(command.time, "sleep", lambda seconds: pytest.fail("should not wait"))
    assert command.main(["--wait", "1"]) == 0
    assert "waiting" not in logs.text


@pytest.mark.parametrize("argv", [["--wait"], ["1", "--wait"], ["--wait", "x"]])
def test_wrong_use_of_wait_exits_2(argv):
    assert command.main(argv) == 2


@responses.activate
def test_the_run_lock_leaves_no_transaction_open(postgres_url, db):
    with psycopg.connect(postgres_url) as conn:
        assert command.take_run_lock(conn)
        assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE


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
    monkeypatch.setattr(command, "take_run_lock", lambda conn: True)
    monkeypatch.setattr(command, "ingest_label", lost)

    assert command.main(["1"]) == 1
    assert "Stopped: the database connection was lost" in logs.text
