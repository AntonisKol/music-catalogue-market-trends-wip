"""The reader login: it can read, and the database refuses anything else."""
import psycopg
import pytest
from psycopg.conninfo import conninfo_to_dict

from ingestion import create_reader_role as command
from ingestion.apply_schema import apply_schema
from ingestion.create_reader_role import create_reader_role
from tests.conftest import FAKE_SETTINGS

WRITES = [
    "INSERT INTO labels (id, name) VALUES (1, 'x')",
    "UPDATE labels SET name = 'x'",
    "DELETE FROM labels",
    "TRUNCATE labels",
    "DROP TABLE labels",
    "CREATE TABLE stolen (id int)",
    "ALTER TABLE labels ADD COLUMN x int",
    "CREATE TEMP TABLE scratch (id int)",
]


def raw_reader(postgres_url, password=FAKE_SETTINGS["POSTGRES_READER_PASSWORD"]):
    """A reader connection without the client's own read-only options: only the database's rules apply."""
    info = conninfo_to_dict(postgres_url)
    return psycopg.connect(
        host=info["host"], port=info["port"], dbname=info["dbname"],
        user=FAKE_SETTINGS["POSTGRES_READER_USER"], password=password, autocommit=True,
    )


def test_reader_can_read(db, reader):
    db.execute("INSERT INTO labels (id, name) VALUES (1, 'Planet E')")
    db.commit()

    assert reader.execute("SELECT name FROM labels").fetchall() == [("Planet E",)]


@pytest.mark.parametrize("statement", WRITES)
def test_read_only_session_blocks_writes(reader, statement):
    with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
        reader.execute(statement)


@pytest.mark.parametrize("statement", WRITES)
def test_writes_are_refused_even_after_switching_read_only_off(reader, statement):
    """The session setting is only the second lock; the role's permissions are the wall."""
    reader.execute("SET default_transaction_read_only = off")

    with pytest.raises(psycopg.errors.InsufficientPrivilege):
        reader.execute(statement)


def test_database_applies_the_limits_without_the_clients_options(reader_role, postgres_url):
    with raw_reader(postgres_url) as conn:
        assert conn.execute("SHOW default_transaction_read_only").fetchone() == ("on",)
        assert conn.execute("SHOW statement_timeout").fetchone() == ("5s",)


def test_the_reader_connection_also_asks_for_the_limits_itself(reader):
    """The second lock: set by the client, not only inherited from the role."""
    rows = reader.execute(
        "SELECT name, setting, source FROM pg_settings"
        " WHERE name IN ('default_transaction_read_only', 'statement_timeout') ORDER BY name"
    ).fetchall()
    assert rows == [("default_transaction_read_only", "on", "client"), ("statement_timeout", "5000", "client")]


def test_statement_timeout_cancels_slow_queries(reader):
    reader.execute("SET statement_timeout = '200ms'")

    with pytest.raises(psycopg.errors.QueryCanceled):
        reader.execute("SELECT pg_sleep(2)")


def test_reader_can_read_but_not_write_tables_added_later(db, reader):
    db.execute("CREATE TABLE IF NOT EXISTS later_table (id int)")
    db.commit()
    try:
        assert reader.execute("SELECT count(*) FROM later_table").fetchone() == (0,)
        reader.execute("SET default_transaction_read_only = off")
        with pytest.raises(psycopg.errors.InsufficientPrivilege):
            reader.execute("INSERT INTO later_table VALUES (1)")
    finally:
        db.execute("DROP TABLE later_table")
        db.commit()


def test_reader_can_read_release_labels_added_by_applying_the_schema(db, reader):
    """As on the server: the reader role exists, then a deploy adds release_labels (issue 94)."""
    db.execute("DROP TABLE release_labels")
    db.commit()
    apply_schema(db)
    db.execute("INSERT INTO labels (id, name) VALUES (1, 'Planet E')")
    db.execute("INSERT INTO releases (id, title, label_id, is_physical) VALUES (10, 'x', 1, true)")
    db.execute("INSERT INTO release_labels (release_id, label_id) VALUES (10, 1)")
    db.commit()

    assert reader.execute("SELECT release_id, label_id FROM release_labels").fetchall() == [(10, 1)]


def test_running_again_changes_the_password(reader_role, postgres_url):
    with psycopg.connect(postgres_url) as conn:
        create_reader_role(conn, reader_role, "new-password")
    try:
        with raw_reader(postgres_url, "new-password") as conn:
            assert conn.execute("SELECT 1").fetchone() == (1,)
        with pytest.raises(psycopg.OperationalError):
            raw_reader(postgres_url)
    finally:
        with psycopg.connect(postgres_url) as conn:
            create_reader_role(conn, reader_role, FAKE_SETTINGS["POSTGRES_READER_PASSWORD"])


@pytest.mark.parametrize("name", ["Reader", "reader; DROP TABLE labels", "1reader", ""])
def test_rejects_unsafe_role_names(postgres_url, name):
    with psycopg.connect(postgres_url) as conn, pytest.raises(ValueError):
        create_reader_role(conn, name, "pw")


def test_command_needs_the_reader_settings(monkeypatch, capsys):
    monkeypatch.delenv("POSTGRES_READER_PASSWORD")

    assert command.main([]) == 1
    assert "POSTGRES_READER_USER and POSTGRES_READER_PASSWORD" in capsys.readouterr().err


def test_command_creates_the_role(postgres_url, monkeypatch, capsys):
    monkeypatch.setattr(command.database, "connect", lambda: psycopg.connect(postgres_url))

    assert command.main([]) == 0
    assert "SELECT only, read-only sessions, 5s query limit" in capsys.readouterr().out
