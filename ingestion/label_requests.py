"""Review the labels visitors asked for, and decide.

    python -m ingestion.label_requests list                       # requests waiting for review
    python -m ingestion.label_requests approve <request> <label>  # a Discogs label id or link
    python -m ingestion.label_requests reject <request> <reason>  # the reason goes to everyone who asked
    python -m ingestion.label_requests notify                     # cron: email new requests, retry failed emails

Each request waits for review until it is approved or rejected, and everyone who
asked is emailed the decision (with the reason, for a rejection).

Requests for equivalent names ("Polegroup", "polegroup records") are one request:
deciding one decides them all. Approving tracks the label; scripts/label-requests
then imports it straight away. Runs in the admin container, as the app's own login.
"""
import re
import sys
from datetime import datetime, timezone

import psycopg
import requests

from ingestion import config, database, discogs_client
from ingestion.track_labels import add_label
from web import mailer
from web.label_requests import comparable

USAGE = "Usage: python -m ingestion.label_requests list | approve <request> <label> | reject <request> <reason> | notify"
MATCHES = 5
MAX_REASON_LENGTH = 500
DISCOGS_LINK = re.compile(r"discogs\.com/(?:[a-z]{2}/)?label/(\d+)")
# How many arguments each command takes: (at least, at most). A reason may be several words.
ARGUMENTS = {"list": (0, 0), "notify": (0, 0), "approve": (2, 2), "reject": (2, None)}


def well_formed(argv: list) -> bool:
    if not argv or argv[0] not in ARGUMENTS:
        return False
    least, most = ARGUMENTS[argv[0]]
    given = len(argv) - 1
    return given >= least and (most is None or given <= most) and (given == 0 or argv[1].isdigit())


def discogs_label_id(text: str) -> int:
    """A Discogs label id, typed as a number or as the label's address on discogs.com."""
    found = DISCOGS_LINK.search(text)
    if found:
        return int(found.group(1))
    if text.isdigit():
        return int(text)
    raise ValueError(f"{text!r} is not a Discogs label id or link.")


def open_requests(conn: psycopg.Connection) -> list:
    """Waiting requests grouped by equivalent name: (first id, name, request ids, first asked)."""
    groups = {}
    for request_id, name, created in conn.execute(
        "SELECT id, name, created_at FROM label_requests WHERE status = 'waiting' ORDER BY id"
    ).fetchall():
        groups.setdefault(comparable(name), [request_id, name, [], created])[2].append(request_id)
    return sorted(groups.values(), key=lambda group: (-len(group[2]), group[3]))


def same_label(conn: psycopg.Connection, request_id: int):
    """(name, ids of every waiting request for an equivalent name), or None if the request isn't waiting."""
    row = conn.execute("SELECT name FROM label_requests WHERE id = %s AND status = 'waiting'", (request_id,)).fetchone()
    if row is None:
        return None
    ids = [group[2] for group in open_requests(conn) if comparable(group[1]) == comparable(row[0])]
    return row[0], ids[0]


def decide(conn: psycopg.Connection, ids: list, status: str, label_id: int = None, reason: str = None) -> None:
    with conn.transaction():
        conn.execute("UPDATE label_requests SET status = %s, label_id = %s, reason = %s, decided_at = %s"
                     " WHERE id = ANY(%s)", (status, label_id, reason, datetime.now(timezone.utc), ids))


def matches_on_discogs(name: str) -> list:
    """[(discogs id, name, entries)] for the best matches; empty if Discogs can't be reached."""
    try:
        return [(match["id"], match["title"], discogs_client.label_size(match["id"]))
                for match in discogs_client.search_labels(name, limit=MATCHES)]
    except requests.RequestException as error:
        print(f"Discogs could not be reached: {error}", file=sys.stderr)
        return []


def email_status_changes(conn: psycopg.Connection) -> int:
    """Email each visitor whose request was decided (added or rejected) and who hasn't
    been told yet. Each decision is emailed once. Returns how many emails failed."""
    failed = 0
    for request_id, email, name, status, reason, label in conn.execute(
        "SELECT r.id, a.email, r.name, r.status, r.reason, t.name FROM label_requests r"
        " JOIN accounts a ON a.id = r.account_id LEFT JOIN tracked_labels t ON t.label_id = r.label_id"
        " WHERE r.status <> 'waiting' AND r.visitor_notified_status IS DISTINCT FROM r.status ORDER BY r.id"
    ).fetchall():
        try:
            mailer.send_label_status(email, name, status, reason=reason, label=label)
        except mailer.MailUnavailable as error:
            print(f"Request {request_id}: the {status} email to the visitor failed: {error}", file=sys.stderr)
            failed += 1
            continue
        with conn.transaction():
            conn.execute("UPDATE label_requests SET visitor_notified_status = %s, visitor_notified_at = now()"
                         " WHERE id = %s", (status, request_id))
    return failed


def emailed(failed: int) -> str:
    """How the visitors' emails went, for the owner's terminal."""
    return f"everyone who asked was emailed{f', except {failed} (retried every 15 minutes)' if failed else ''}"


def notify(conn: psycopg.Connection) -> int:
    """Email the reviewer each new request, and retry visitors' emails. Returns how many failed."""
    failed = email_status_changes(conn)
    if not config.LABEL_REQUEST_EMAIL:
        return failed
    for first_id, name, ids, _created in open_requests(conn):
        new = conn.execute("SELECT count(*) FROM label_requests WHERE id = ANY(%s) AND owner_notified_at IS NULL",
                           (ids,)).fetchone()[0]
        if not new:
            continue
        try:
            mailer.send_label_request(config.LABEL_REQUEST_EMAIL, first_id, name, len(ids), matches_on_discogs(name))
        except mailer.MailUnavailable as error:
            print(f"Request {first_id}: the email to the reviewer failed: {error}", file=sys.stderr)
            failed += 1
            continue
        with conn.transaction():
            conn.execute("UPDATE label_requests SET owner_notified_at = now() WHERE id = ANY(%s)", (ids,))
        print(f"Emailed the reviewer about request {first_id}: {name}.")
    return failed


def main(argv: list) -> int:
    if not well_formed(argv):
        print(USAGE, file=sys.stderr)
        return 2
    command, args = argv[0], argv[1:]
    try:
        with database.connect() as conn:
            if command == "list":
                groups = open_requests(conn)
                if not groups:
                    print("No label requests are waiting.")
                for first_id, name, ids, created in groups:
                    asked = f"{len(ids)} {'account' if len(ids) == 1 else 'accounts'}"
                    print(f"{first_id:>6}  {name}  ({asked}, since {created:%Y-%m-%d})")
                return 0
            if command == "notify":
                return 1 if notify(conn) else 0
            found = same_label(conn, int(args[0]))
            if found is None:
                print(f"Request {args[0]} is not waiting for review.", file=sys.stderr)
                return 1
            name, ids = found
            if command == "reject":
                reason = " ".join(args[1:]).strip()
                if not 3 <= len(reason) <= MAX_REASON_LENGTH:
                    print(f"Give a reason of 3 to {MAX_REASON_LENGTH} characters.", file=sys.stderr)
                    return 2
                decide(conn, ids, "rejected", reason=reason)
                print(f"Rejected {len(ids)} request(s) for {name}; {emailed(email_status_changes(conn))}, "
                      "with the reason.")
                return 0
            label_id = discogs_label_id(args[1])
            title = add_label(conn, label_id, added_by="request")
            decide(conn, ids, "added", label_id=label_id)
            print(f"Tracking {label_id}: {title}. Approved {len(ids)} request(s) for {name}; "
                  f"{emailed(email_status_changes(conn))}.")
            return 0
    except ValueError as error:
        print(error, file=sys.stderr)
        return 2
    except requests.HTTPError as error:
        missing = error.response is not None and error.response.status_code == 404
        print("That label is not on Discogs." if missing else f"Discogs refused: {error}", file=sys.stderr)
    except requests.RequestException as error:
        print(f"Discogs could not be reached: {error}", file=sys.stderr)
    except psycopg.Error as error:
        print(f"Database error: {error}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
