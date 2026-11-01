"""Shared test setup.

ingestion/config.py stops at import when a setting is missing, which is what the
app should do. Tests set obviously fake values here, before any ingestion module
is imported, so they never need or use the real .env. load_dotenv() does not
override variables that are already set.

Database tests get a throwaway PostgreSQL 16 container (Testcontainers) with
db/schema.sql loaded, shared by the whole run. Each test starts with every table
emptied, and its writes are committed for real, as in the app.
"""
import os
from pathlib import Path

import pytest

FAKE_SETTINGS = {
    "DISCOGS_TOKEN": "test-token",
    "DISCOGS_USER_AGENT": "MusicCatalogueMarketTrendsTests/0.1",
    "POSTGRES_HOST": "localhost",
    "POSTGRES_PORT": "5433",
    "POSTGRES_USER": "test",
    "POSTGRES_PASSWORD": "test",
    "POSTGRES_DB": "test",
    "POSTGRES_READER_USER": "catalogue_reader",
    "POSTGRES_READER_PASSWORD": "reader-test-password",
}

os.environ.update(FAKE_SETTINGS)

SCHEMA = Path(__file__).resolve().parent.parent / "db" / "schema.sql"
TABLES = "release_artists, price_snapshots, demand_snapshots, releases, artists, labels"


def docker_is_running() -> bool:
    import docker

    try:
        docker.from_env().ping()
    except docker.errors.DockerException:
        return False
    return True


@pytest.fixture(scope="session")
def postgres_url():
    """Connection URL of a throwaway PostgreSQL with the schema, removed after the run.

    Without Docker the database tests are skipped on a laptop, but fail in CI
    (CI=true), so a CI run can never pass without them.
    """
    import psycopg
    from testcontainers.community.postgres import PostgresContainer

    if not docker_is_running():
        message = "Docker is not running, so the database tests cannot start PostgreSQL"
        if os.environ.get("CI"):
            pytest.fail(message)
        pytest.skip(message)

    # Testcontainers' clean-up container mounts the Docker socket. With Colima the
    # default host path does not exist inside the VM; this path works there and
    # with Docker Desktop and Linux.
    os.environ.setdefault("TESTCONTAINERS_DOCKER_SOCKET_OVERRIDE", "/var/run/docker.sock")
    with PostgresContainer("postgres:16", driver=None) as container:
        url = container.get_connection_url()
        with psycopg.connect(url, autocommit=True) as conn:
            conn.execute(SCHEMA.read_text())
        yield url


@pytest.fixture
def db(postgres_url):
    """A connection like database.connect() gives, to an empty database."""
    import psycopg

    with psycopg.connect(postgres_url, autocommit=True) as admin:
        admin.execute(f"TRUNCATE {TABLES} RESTART IDENTITY")
    with psycopg.connect(postgres_url) as conn:
        yield conn


@pytest.fixture(autouse=True)
def discogs_clock(monkeypatch):
    """A fake clock for the Discogs client: pauses and retry waits are recorded, not waited.

    Returns the list of waits, in seconds, in the order they happened.
    """
    from ingestion import discogs_client

    clock = {"now": 0.0}
    waits = []

    def sleep(seconds):
        waits.append(seconds)
        clock["now"] += seconds

    monkeypatch.setattr(discogs_client, "_sleep", sleep)
    monkeypatch.setattr(discogs_client, "_now", lambda: clock["now"])
    monkeypatch.setattr(discogs_client, "_last_request", None)
    return waits


@pytest.fixture(scope="session")
def reader_role(postgres_url):
    """The read-only login, created in the test database exactly as in production."""
    import psycopg

    from ingestion.create_reader_role import create_reader_role

    with psycopg.connect(postgres_url) as conn:
        create_reader_role(conn, FAKE_SETTINGS["POSTGRES_READER_USER"], FAKE_SETTINGS["POSTGRES_READER_PASSWORD"])
    return FAKE_SETTINGS["POSTGRES_READER_USER"]


@pytest.fixture
def reader(db, reader_role, postgres_url, monkeypatch):
    """A connection from queries.database.connect_reader(), pointed at the test database."""
    from psycopg.conninfo import conninfo_to_dict

    from queries import config, database

    info = conninfo_to_dict(postgres_url)
    monkeypatch.setattr(config, "POSTGRES_HOST", info["host"])
    monkeypatch.setattr(config, "POSTGRES_PORT", str(info["port"]))
    monkeypatch.setattr(config, "POSTGRES_DB", info["dbname"])
    with database.connect_reader() as conn:
        yield conn
