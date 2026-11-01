"""The save_release command: exit codes and messages, with Discogs faked."""
import psycopg
import pytest
import responses

from ingestion import save_release as command
from ingestion.discogs_client import BASE_URL

RELEASE = {
    "id": 249504,
    "title": "Never Gonna Give You Up",
    "year": 1987,
    "labels": [{"id": 895, "name": "RCA", "catno": "PB 41447"}],
    "artists": [{"id": 72872, "name": "Rick Astley"}],
    "formats": [{"name": "Vinyl"}],
}


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
def test_saves_and_prints_the_release(postgres_url, db, connect_to, capsys):
    responses.get(f"{BASE_URL}/releases/249504", json=RELEASE)
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["249504"]) == 0
    assert capsys.readouterr().out == (
        "Saved 249504: Rick Astley - Never Gonna Give You Up (1987) on RCA\n"
    )


@responses.activate
def test_discogs_failure(connect_to, capsys):
    responses.get(f"{BASE_URL}/releases/249504", status=404)
    connect_to(lambda: pytest.fail("must not connect when Discogs failed"))

    assert command.main(["249504"]) == 1
    assert "Discogs request for release 249504 failed" in capsys.readouterr().err


@responses.activate
def test_database_unreachable(connect_to, capsys):
    responses.get(f"{BASE_URL}/releases/249504", json=RELEASE)

    def refuse():
        raise psycopg.OperationalError("connection refused")

    connect_to(refuse)

    assert command.main(["249504"]) == 1
    assert "Saving release 249504 to PostgreSQL failed: connection refused" in capsys.readouterr().err


@responses.activate
def test_unmappable_release(postgres_url, db, connect_to, capsys):
    responses.get(f"{BASE_URL}/releases/249504", json={"id": 249504})
    connect_to(lambda: psycopg.connect(postgres_url))

    assert command.main(["249504"]) == 1
    assert "could not be mapped" in capsys.readouterr().err
