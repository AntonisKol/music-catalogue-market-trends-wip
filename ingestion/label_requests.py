"""Look at the labels visitors asked for, and decide.

    python -m ingestion.label_requests list               # waiting requests, by name
    python -m ingestion.label_requests search <request>   # matching labels on Discogs, with their size
    python -m ingestion.label_requests approve <request> <discogs_label_id>
    python -m ingestion.label_requests decline <request>

Approving tracks the label (added by "request"); its data arrives with the next
nightly run. Approving or declining decides every waiting request for the same
name at once. Runs as the app's own login, like track_labels.
"""
import sys
from datetime import datetime, timezone

import psycopg
import requests

from ingestion import database, discogs_client
from ingestion.track_labels import add_label

USAGE = ("Usage: python -m ingestion.label_requests list | search <request> | "
         "approve <request> <discogs_label_id> | decline <request>")


def waiting(conn: psycopg.Connection) -> list:
    """(first request id, name, how many accounts asked, first asked) per name."""
    return conn.execute(
        "SELECT min(id), (array_agg(name ORDER BY id))[1], count(*), min(created_at) FROM label_requests"
        " WHERE status = 'waiting'"
        " GROUP BY lower(name) ORDER BY count(*) DESC, min(created_at)"
    ).fetchall()


def request_name(conn: psycopg.Connection, request_id: int):
    row = conn.execute("SELECT name FROM label_requests WHERE id = %s", (request_id,)).fetchone()
    return row[0] if row else None


def decide(conn: psycopg.Connection, name: str, status: str, label_id: int = None, now: datetime = None) -> int:
    """Mark every waiting request for this name; returns how many."""
    with conn.transaction():
        return conn.execute(
            "UPDATE label_requests SET status = %s, label_id = %s, decided_at = %s"
            " WHERE lower(name) = lower(%s) AND status = 'waiting'",
            (status, label_id, now or datetime.now(timezone.utc), name),
        ).rowcount


def main(argv: list) -> int:
    command = argv[0] if argv else ""
    shapes = {"list": 0, "search": 1, "decline": 1, "approve": 2}
    if command not in shapes or len(argv) - 1 != shapes[command] or not all(a.isdigit() for a in argv[1:]):
        print(USAGE, file=sys.stderr)
        return 2
    try:
        with database.connect() as conn:
            if command == "list":
                rows = waiting(conn)
                if not rows:
                    print("No label requests are waiting.")
                for request_id, name, accounts, first in rows:
                    print(f"{request_id:>6}  {name}  ({accounts} {'account' if accounts == 1 else 'accounts'}, since {first:%Y-%m-%d})")
                return 0
            name = request_name(conn, int(argv[1]))
            if name is None:
                print(f"There is no request {argv[1]}.", file=sys.stderr)
                return 1
            if command == "search":
                matches = discogs_client.search_labels(name)
                if not matches:
                    print(f"Discogs has no label matching {name!r}.")
                for match in matches:
                    print(f"{match['id']:>8}  {match['title']}  ({discogs_client.label_size(match['id'])} entries)")
                return 0
            if command == "decline":
                print(f"Declined {decide(conn, name, 'declined')} request(s) for {name}.")
                return 0
            label_id = int(argv[2])
            title = add_label(conn, label_id, added_by="request")
            print(f"Tracking {label_id}: {title}. Approved {decide(conn, name, 'added', label_id)} request(s) for {name}.")
            return 0
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
