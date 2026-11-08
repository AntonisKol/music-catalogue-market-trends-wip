"""Review the labels visitors asked for, and decide.

    python -m ingestion.label_requests list                       # requests waiting or being reviewed
    python -m ingestion.label_requests review <request>           # Discogs matches; marks it "being reviewed"
    python -m ingestion.label_requests approve <request> <label>  # a Discogs label id or link
    python -m ingestion.label_requests reject <request> <reason>  # emails the reason to everyone who asked
    python -m ingestion.label_requests notify                     # cron: email new requests, retry failed emails

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

USAGE = ("Usage: python -m ingestion.label_requests list | review <request> | approve <request> <label> | "
         "reject <request> <reason> | notify")
OPEN = ("waiting", "reviewing")
MATCHES = 5
MAX_REASON_LENGTH = 500
DISCOGS_LINK = re.compile(r"discogs\.com/(?:[a-z]{2}/)?label/(\d+)")
# How many arguments each command takes: (at least, at most). A reason may be several words.
ARGUMENTS = {"list": (0, 0), "notify": (0, 0), "review": (1, 1), "approve": (2, 2), "reject": (2, None)}


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
    """Open requests grouped by equivalent name: (first id, name, status, request ids, first asked)."""
    groups = {}
    for request_id, name, status, created in conn.execute(
        "SELECT id, name, status, created_at FROM label_requests WHERE status = ANY(%s) ORDER BY id", (list(OPEN),)
    ).fetchall():
        group = groups.setdefault(comparable(name), [request_id, name, status, [], created])
        group[3].append(request_id)
        if status == "reviewing":
            group[2] = "reviewing"
    return sorted(groups.values(), key=lambda group: (-len(group[3]), group[4]))


def same_label(conn: psycopg.Connection, request_id: int):
    """(name, ids of every open request for an equivalent name), or None if the request isn't open."""
    row = conn.execute("SELECT name FROM label_requests WHERE id = %s AND status = ANY(%s)",
                       (request_id, list(OPEN))).fetchone()
    if row is None:
        return None
    ids = [group[3] for group in open_requests(conn) if comparable(group[1]) == comparable(row[0])]
    return row[0], ids[0]


def set_status(conn: psycopg.Connection, ids: list, status: str, label_id: int = None, reason: str = None) -> None:
    decided = None if status == "reviewing" else datetime.now(timezone.utc)
    with conn.transaction():
        conn.execute("UPDATE label_requests SET status = %s, label_id = %s, reason = %s, decided_at = %s"
                     " WHERE id = ANY(%s)", (status, label_id, reason, decided, ids))


def matches_on_discogs(name: str) -> list:
    """[(discogs id, name, entries)] for the best matches; empty if Discogs can't be reached."""
    try:
        return [(match["id"], match["title"], discogs_client.label_size(match["id"]))
                for match in discogs_client.search_labels(name, limit=MATCHES)]
    except requests.RequestException as error:
        print(f"Discogs could not be reached: {error}", file=sys.stderr)
        return []


def email_rejections(conn: psycopg.Connection) -> int:
    """Email each rejected visitor the reason, once. Returns how many emails failed."""
    failed = 0
    for request_id, email, name, reason in conn.execute(
        "SELECT r.id, a.email, r.name, r.reason FROM label_requests r JOIN accounts a ON a.id = r.account_id"
        " WHERE r.status = 'rejected' AND r.reason IS NOT NULL AND r.visitor_notified_at IS NULL ORDER BY r.id"
    ).fetchall():
        try:
            mailer.send_label_rejected(email, name, reason)
        except mailer.MailUnavailable as error:
            print(f"Request {request_id}: the rejection email failed: {error}", file=sys.stderr)
            failed += 1
            continue
        with conn.transaction():
            conn.execute("UPDATE label_requests SET visitor_notified_at = now() WHERE id = %s", (request_id,))
    return failed


def notify(conn: psycopg.Connection) -> int:
    """Email the reviewer each new request, and retry rejection emails. Returns how many failed."""
    failed = email_rejections(conn)
    if not config.LABEL_REQUEST_EMAIL:
        return failed
    for first_id, name, _status, ids, _created in open_requests(conn):
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
                for first_id, name, status, ids, created in groups:
                    asked = f"{len(ids)} {'account' if len(ids) == 1 else 'accounts'}"
                    print(f"{first_id:>6}  {name}  ({asked}, since {created:%Y-%m-%d}"
                          f"{', being reviewed' if status == 'reviewing' else ''})")
                return 0
            if command == "notify":
                return 1 if notify(conn) else 0
            found = same_label(conn, int(args[0]))
            if found is None:
                print(f"Request {args[0]} is not open.", file=sys.stderr)
                return 1
            name, ids = found
            if command == "review":
                set_status(conn, ids, "reviewing")
                print(f"{name}: {len(ids)} request(s), now shown as being reviewed. Labels on Discogs:")
                for label_id, title, entries in matches_on_discogs(name):
                    print(f"{label_id:>8}  {title}  ({entries} entries)  https://www.discogs.com/label/{label_id}")
                return 0
            if command == "reject":
                reason = " ".join(args[1:]).strip()
                if not 3 <= len(reason) <= MAX_REASON_LENGTH:
                    print(f"Give a reason of 3 to {MAX_REASON_LENGTH} characters.", file=sys.stderr)
                    return 2
                set_status(conn, ids, "rejected", reason=reason)
                failed = email_rejections(conn)
                print(f"Rejected {len(ids)} request(s) for {name}; the reason was emailed"
                      + (f", except {failed}, which will be retried." if failed else "."))
                return 0
            label_id = discogs_label_id(args[1])
            title = add_label(conn, label_id, added_by="request")
            set_status(conn, ids, "added", label_id=label_id)
            print(f"Tracking {label_id}: {title}. Approved {len(ids)} request(s) for {name}.")
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
