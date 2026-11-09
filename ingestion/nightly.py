"""Update every tracked label: tonight's prices, and details once a week.

    python -m ingestion.nightly

Started each night by cron on the server (deploy/crontab). It takes the same lock as ingest_label, so it never runs alongside
another ingestion run. For each active label in tracked_labels it runs the label
pipeline, and records the run and every label's result in ingestion_runs and
ingestion_run_labels. A label that fails does not stop the others. If another run
holds the lock, this run is recorded as skipped and does not start.

Exit 0 when every label was updated without failures, 1 otherwise (the run is
recorded as failed and the problems are logged). Rerunning on the
same day is safe: it updates the same rows. Progress goes to stderr and to a
rotating log file (logs/nightly.log, or LOG_FILE).
"""
import logging
import os
import sys
from datetime import date, datetime, timedelta, timezone
from logging.handlers import RotatingFileHandler
from pathlib import Path

import psycopg
import requests

from ingestion import database
from ingestion.ingest_label import take_run_lock
from ingestion.pipeline import DatabaseConnectionLost, ingest_label
from ingestion.track_labels import tracked_labels

log = logging.getLogger("ingestion.nightly")

DEFAULT_LOG_FILE = Path(__file__).resolve().parent.parent / "logs" / "nightly.log"
# Nights were missed if the previous run, whatever its outcome, started longer ago than this.
MISSED_AFTER = timedelta(hours=36)


def start_run(conn: psycopg.Connection, snapshot_date: date) -> int:
    with conn.transaction():
        # Only one run holds the lock, so any run still marked running was interrupted.
        conn.execute(
            "UPDATE ingestion_runs SET status = 'failed', finished_at = now(), "
            "message = 'interrupted before it finished' WHERE status = 'running'"
        )
        return conn.execute(
            "INSERT INTO ingestion_runs (snapshot_date, status) VALUES (%s, 'running') RETURNING id", (snapshot_date,)
        ).fetchone()[0]


def record_skipped(conn: psycopg.Connection, snapshot_date: date) -> str:
    """Record a run that did not start because another ingestion run held the lock.

    Unlike start_run it leaves any running row alone: that run really is still going."""
    with conn.transaction():
        running = conn.execute(
            "SELECT started_at FROM ingestion_runs WHERE status = 'running' ORDER BY started_at DESC LIMIT 1"
        ).fetchone()
        if running:
            reason = f"the nightly run started {running[0].astimezone(timezone.utc):%Y-%m-%d %H:%M} UTC was still going"
        else:
            reason = "another ingestion run (a label import or adding labels) was still going"
        conn.execute(
            "INSERT INTO ingestion_runs (snapshot_date, status, finished_at, message) "
            "VALUES (%s, 'skipped', now(), %s)",
            (snapshot_date, f"Not started: {reason}."),
        )
    return reason


def last_run(conn: psycopg.Connection):
    """When the previous run started, whatever its outcome. Skipped runs updated
    nothing, so they don't count: a skipped night is still a missed night."""
    with conn.transaction():
        row = conn.execute("SELECT max(started_at) FROM ingestion_runs WHERE status <> 'skipped'").fetchone()
    return row[0]


def record_label(conn, run_id: int, label_id: int, result=None, error: str = None) -> None:
    counts = (0, 0, 0, 0, 0, 0) if result is None else (
        result.listed, len(result.saved), len(result.not_vinyl), len(result.not_on_label),
        len(result.gone), len(result.failed),
    )
    with conn.transaction():
        conn.execute(
            """
            INSERT INTO ingestion_run_labels
                (run_id, label_id, listed, saved, not_vinyl, not_on_label, gone, failed, error)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (run_id, label_id, *counts, error),
        )


def finish_run(conn, run_id: int, ok: bool, message: str, warning: str = None) -> None:
    with conn.transaction():
        conn.execute(
            "UPDATE ingestion_runs SET status = %s, finished_at = now(), message = %s, warning = %s WHERE id = %s",
            ("succeeded" if ok else "failed", message, warning, run_id),
        )


def run_nightly(conn: psycopg.Connection, snapshot_date: date, now: datetime) -> tuple:
    """Update every active tracked label. Returns (ok, summary, problems, warning).

    ok and problems are about this run only. Missed nights are a warning: they say
    nothing about whether tonight's update worked."""
    problems = []
    warning = None
    previous = last_run(conn)
    if previous is not None and now - previous > MISSED_AFTER:
        warning = f"No nightly update started between {previous:%Y-%m-%d %H:%M} UTC and this one; nights were missed."
    run_id = start_run(conn, snapshot_date)
    labels = tracked_labels(conn, active_only=True)
    if not labels:
        log.warning("No active tracked labels; add some with python -m ingestion.track_labels add <id>")
    totals = {"saved": 0, "failed": 0}
    for number, (label_id, name, *_rest) in enumerate(labels, start=1):
        log.info("[label %s/%s] %s (%s)", number, len(labels), name, label_id)
        try:
            result = ingest_label(conn, label_id, snapshot_date)
        except requests.RequestException as error:
            log.error("Label %s (%s): Discogs list failed: %s", label_id, name, error)
            record_label(conn, run_id, label_id, error=str(error))
            problems.append(f"{name} ({label_id}): the label list could not be fetched: {error}")
            continue
        except DatabaseConnectionLost as error:
            problems.append(f"The database connection was lost during {name} ({label_id}): {error}")
            raise
        record_label(conn, run_id, label_id, result)
        totals["saved"] += len(result.saved)
        totals["failed"] += len(result.failed)
        log.info(
            "Label %s (%s): %s listed, %s saved (%s with details), %s not vinyl, %s not on this label, %s gone, %s failed",
            label_id, name, result.listed, len(result.saved), len(result.with_details), len(result.not_vinyl),
            len(result.not_on_label), len(result.gone), len(result.failed),
        )
        if result.failed:
            problems.append(f"{name} ({label_id}): {len(result.failed)} releases failed, e.g. "
                            + "; ".join(f"{rid}: {err}" for rid, err in list(result.failed.items())[:3]))
    summary = f"{len(labels)} labels, {totals['saved']} releases saved, {totals['failed']} failed"
    ok = not problems
    finish_run(conn, run_id, ok, summary if ok else summary + ". " + " ".join(problems), warning)
    return ok, summary, problems, warning


def configure_logging() -> None:
    log_file = Path(os.environ.get("LOG_FILE") or DEFAULT_LOG_FILE)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handlers = [logging.StreamHandler(), RotatingFileHandler(log_file, maxBytes=5_000_000, backupCount=10)]
    for handler in handlers:
        handler.setFormatter(formatter)
    logging.basicConfig(level=logging.INFO, handlers=handlers)


def main(argv: list) -> int:
    if argv:
        print("Usage: python -m ingestion.nightly", file=sys.stderr)
        return 2
    now = datetime.now(timezone.utc)
    snapshot_date = now.date()
    log.info("Nightly run, snapshot date %s (UTC)", snapshot_date)
    try:
        with database.connect() as conn:
            if not take_run_lock(conn):
                reason = record_skipped(conn, snapshot_date)
                log.error("Another ingestion run is in progress; not starting the nightly run (%s)", reason)
                return 1
            ok, summary, problems, warning = run_nightly(conn, snapshot_date, now)
    except DatabaseConnectionLost as error:
        log.error("Stopped: the database connection was lost: %s", error)
        return 1
    except psycopg.Error as error:
        log.error("Could not use PostgreSQL: %s", error)
        return 1
    if warning:
        log.warning(warning)
    if ok:
        log.info("Nightly run done: %s", summary)
        return 0
    log.error("Nightly run finished with problems: %s", summary)
    for problem in problems:
        log.error("  %s", problem)
    return 1


if __name__ == "__main__":
    configure_logging()
    sys.exit(main(sys.argv[1:]))
