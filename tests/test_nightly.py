"""The nightly run: tracked labels, run records, missed nights, logging, and the schema command."""
import logging
from datetime import date, datetime, timedelta, timezone

import psycopg
import pytest
import responses
from responses import matchers

from ingestion import apply_schema, nightly, runs, track_labels
from ingestion.discogs_client import BASE_URL
from ingestion.ingest_label import RUN_LOCK_ID
from ingestion.nightly import run_nightly
from ingestion.track_labels import add_label, set_active

DAY = date(2026, 11, 2)
NOW = datetime(2026, 11, 2, 2, 0, tzinfo=timezone.utc)


def discogs_label(label_id, name, releases=(), list_status=200):
    responses.get(f"{BASE_URL}/labels/{label_id}", json={"id": label_id, "name": name})
    responses.get(
        f"{BASE_URL}/labels/{label_id}/releases",
        json={"pagination": {"page": 1, "pages": 1}, "releases": [{"id": r, "format": '12"'} for r in releases]}
        if list_status == 200 else {"message": "error"},
        status=list_status,
    )
    for release_id in releases:
        responses.get(f"{BASE_URL}/releases/{release_id}", json={
            "id": release_id, "title": f"Release {release_id}", "labels": [{"id": label_id, "name": name}],
            "formats": [{"name": "Vinyl"}], "community": {"want": 1, "have": 1},
        })
        responses.get(f"{BASE_URL}/marketplace/stats/{release_id}", match=[matchers.query_param_matcher({"curr_abbr": "EUR"})],
                      json={"num_for_sale": 1, "lowest_price": {"value": 9.5, "currency": "EUR"}})


def query(db, sql, params=None):
    db.commit()
    rows = db.execute(sql, params).fetchall()
    db.commit()
    return rows


# --- Tracked labels ------------------------------------------------------------------

@responses.activate
def test_adding_a_label_records_its_discogs_name(db):
    discogs_label(538, "Perlon")

    assert add_label(db, 538) == "Perlon"
    assert query(db, "SELECT label_id, name, added_by, active FROM tracked_labels") == [(538, "Perlon", "site", True)]


@responses.activate
def test_pausing_and_readding_a_label(db):
    discogs_label(538, "Perlon")
    add_label(db, 538)

    assert set_active(db, 538, False) is True
    assert query(db, "SELECT active FROM tracked_labels") == [(False,)]
    add_label(db, 538)
    assert query(db, "SELECT active FROM tracked_labels") == [(True,)]
    assert set_active(db, 999, False) is False


@responses.activate
def test_the_command_reports_unknown_labels(db, postgres_url, monkeypatch, capsys):
    monkeypatch.setattr(track_labels.database, "connect", lambda: psycopg.connect(postgres_url))
    discogs_label(538, "Perlon")
    responses.get(f"{BASE_URL}/labels/999999999", json={"message": "Label not found."}, status=404)

    assert track_labels.main(["add", "538", "999999999"]) == 1
    out = capsys.readouterr()
    assert "Tracking 538: Perlon" in out.out and "Label 999999999: not found on Discogs" in out.err
    assert track_labels.main(["list"]) == 0
    assert "538  Perlon  (active" in capsys.readouterr().out


@pytest.mark.parametrize("argv", [[], ["add"], ["list", "1"], ["add", "x"], ["remove", "1"]])
def test_track_labels_usage(argv, capsys):
    assert track_labels.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


# --- The nightly run ------------------------------------------------------------------

@responses.activate
def test_every_active_label_is_updated_and_recorded(db):
    discogs_label(1, "Planet E", releases=[11, 12])
    discogs_label(538, "Perlon", releases=[21])
    discogs_label(7, "Paused Label", releases=[31])
    for label_id in (1, 538, 7):
        add_label(db, label_id)
    set_active(db, 7, False)

    ok, summary, problems = run_nightly(db, DAY, NOW)

    assert ok and problems == []
    assert summary == "2 labels, 3 releases saved, 0 failed"
    assert query(db, "SELECT count(*) FROM price_snapshots WHERE snapshot_date = %s", (DAY,)) == [(3,)]
    assert query(db, "SELECT status, snapshot_date, finished_at IS NOT NULL FROM ingestion_runs") == [("succeeded", DAY, True)]
    assert query(db, "SELECT label_id, listed, saved, failed, error FROM ingestion_run_labels ORDER BY label_id") == [
        (1, 2, 2, 0, None), (538, 1, 1, 0, None),
    ]
    assert not any("/labels/7/releases" in call.request.url for call in responses.calls)


@responses.activate
def test_a_failing_label_does_not_stop_the_others(db):
    # Labels run in name order, so the failing one ("Axis") comes first.
    discogs_label(4, "Axis", list_status=500)
    discogs_label(538, "Perlon", releases=[21])
    add_label(db, 4)
    add_label(db, 538)

    ok, summary, problems = run_nightly(db, DAY, NOW)

    assert not ok
    assert "Axis (4): the label list could not be fetched" in problems[0]
    assert query(db, "SELECT label_id, saved, error IS NOT NULL FROM ingestion_run_labels ORDER BY label_id") == [
        (4, 0, True), (538, 1, False),
    ]
    [(status, message)] = query(db, "SELECT status, message FROM ingestion_runs")
    assert status == "failed" and "Axis" in message


@responses.activate
def test_failed_releases_make_the_run_fail_with_examples(db):
    discogs_label(538, "Perlon", releases=[21])
    responses.replace(responses.GET, f"{BASE_URL}/releases/21", json={"message": "x"}, status=401)
    add_label(db, 538)

    ok, _, problems = run_nightly(db, DAY, NOW)

    assert not ok and "Perlon (538): 1 releases failed, e.g. 21:" in problems[0]


def test_a_missed_night_is_reported(db):
    db.execute("INSERT INTO ingestion_runs (snapshot_date, started_at, status) VALUES (%s, %s, 'succeeded')",
               (DAY - timedelta(days=3), NOW - timedelta(days=3)))
    db.commit()

    ok, _, problems = run_nightly(db, DAY, NOW)

    assert not ok and "nights were missed" in problems[0]


def test_last_nights_run_is_not_a_missed_night(db):
    db.execute("INSERT INTO ingestion_runs (snapshot_date, started_at, status) VALUES (%s, %s, 'succeeded')",
               (DAY - timedelta(days=1), NOW - timedelta(hours=24)))
    db.commit()

    assert run_nightly(db, DAY, NOW)[0] is True


def test_an_interrupted_run_is_marked_failed_by_the_next(db):
    db.execute("INSERT INTO ingestion_runs (snapshot_date, status) VALUES (%s, 'running')", (DAY,))
    db.commit()

    run_nightly(db, DAY, NOW)

    assert query(db, "SELECT status, message FROM ingestion_runs ORDER BY id") == [
        ("failed", "interrupted before it finished"), ("succeeded", "0 labels, 0 releases saved, 0 failed"),
    ]


def test_no_tracked_labels_is_a_quiet_success(db, caplog):
    assert run_nightly(db, DAY, NOW)[0] is True
    assert "No active tracked labels" in caplog.text


# --- The command: lock, exit codes, logging --------------------------------------------

@pytest.fixture
def use_test_database(postgres_url, monkeypatch):
    monkeypatch.setattr(nightly.database, "connect", lambda: psycopg.connect(postgres_url))


@responses.activate
def test_a_good_night_exits_0(db, use_test_database):
    discogs_label(538, "Perlon", releases=[21])
    add_label(db, 538)

    assert nightly.main([]) == 0


@responses.activate
def test_a_bad_night_exits_1_and_logs_the_problems(db, use_test_database, caplog):
    discogs_label(538, "Perlon", list_status=500)
    add_label(db, 538)

    assert nightly.main([]) == 1
    assert "finished with problems" in caplog.text and "Perlon (538)" in caplog.text


def test_a_second_run_does_not_start_and_logs_it(db, postgres_url, use_test_database, caplog):
    with psycopg.connect(postgres_url, autocommit=True) as other:
        other.execute("SELECT pg_advisory_lock(%s)", (RUN_LOCK_ID,))
        assert nightly.main([]) == 1
    assert "not starting the nightly run" in caplog.text
    assert query(db, "SELECT count(*) FROM ingestion_runs") == [(0,)]


def test_an_unreachable_database_is_logged(monkeypatch, caplog):
    def refuse():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(nightly.database, "connect", refuse)

    assert nightly.main([]) == 1
    assert "Could not use PostgreSQL" in caplog.text


def test_the_log_file_is_written(tmp_path, monkeypatch):
    log_file = tmp_path / "logs" / "nightly.log"
    monkeypatch.setenv("LOG_FILE", str(log_file))
    root = logging.getLogger()
    saved = root.handlers[:]
    root.handlers = []
    try:
        nightly.configure_logging()
        logging.getLogger("ingestion.nightly").info("hello from the test")
        for handler in root.handlers:
            handler.flush()
        assert "INFO hello from the test" in log_file.read_text()
    finally:
        for handler in root.handlers:
            handler.close()
        root.handlers = saved


# --- Showing runs ------------------------------------------------------------------------

def test_runs_shows_recent_runs_with_failures(db, postgres_url, monkeypatch, capsys):
    db.execute("INSERT INTO ingestion_runs (snapshot_date, started_at, finished_at, status, message) VALUES "
               "(%s, %s, %s, 'failed', 'Perlon (538): the label list could not be fetched')",
               (DAY, NOW, NOW + timedelta(minutes=12)))
    db.commit()
    monkeypatch.setattr(runs.database, "connect", lambda: psycopg.connect(postgres_url))

    assert runs.main([]) == 0
    out = capsys.readouterr().out
    assert "2026-11-02 02:00 UTC  failed" in out and "(12 min)" in out and "Perlon (538)" in out


# --- Applying the schema to an existing database ----------------------------------------

def test_applying_the_schema_again_keeps_data_and_adds_nothing_twice(db):
    db.execute("INSERT INTO labels (id, name) VALUES (1, 'Planet E')")
    db.commit()

    apply_schema.apply_schema(db)
    apply_schema.apply_schema(db)

    assert query(db, "SELECT name FROM labels") == [("Planet E",)]


def test_applying_the_schema_adds_new_tables_to_an_old_database(db):
    db.execute("DROP TABLE ingestion_run_labels")
    db.commit()

    apply_schema.apply_schema(db)

    assert query(db, "SELECT to_regclass('ingestion_run_labels') IS NOT NULL") == [(True,)]
