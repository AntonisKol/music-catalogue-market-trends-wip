"""Label requests (issue 37): signed-in visitors ask for labels, the owner decides.

The web API never calls Discogs; the owner's command does, and Discogs is faked
here with responses. Everything else runs against the real test database.
"""
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest
import responses

from ingestion import discogs_client, label_requests as owner
from ingestion.track_labels import tracked_labels
from tests.test_sign_in import NOON, Clock, Fakes, ask, clock, client, fakes, sign_in  # noqa: F401 (fixtures)
from web import accounts, config, label_requests, limits, mailer

DISCOGS = discogs_client.BASE_URL


def request_label(client, name="Orbe Records"):
    return client.post("/api/label-requests", json={"name": name})


# --- Names --------------------------------------------------------------------------

@pytest.mark.parametrize("typed, expected", [("  Orbe   Records ", "Orbe Records"), ("Arts", "Arts")])
def test_names_are_tidied(typed, expected):
    assert label_requests.clean_name(typed) == expected


@pytest.mark.parametrize("typed", ["", " ", "x", "y" * 101])
def test_names_must_be_2_to_100_characters(typed):
    with pytest.raises(ValueError):
        label_requests.clean_name(typed)


@pytest.mark.parametrize("typed, tracked", [("planet rhythm", "Planet Rhythm Records"), ("Wax", "Wax (4)"),
                                            ("NACHTSTROM", "Nachtstrom Schallplatten")])
def test_a_name_matches_the_tracked_label_it_means(typed, tracked):
    assert label_requests.tracked_match(typed, ["Arts", tracked]) == tracked


def test_a_different_name_is_not_a_match():
    assert label_requests.tracked_match("Planet E Communications", ["Planet Rhythm Records", "Planet E"]) is None


# --- Through the web API ----------------------------------------------------------

def test_only_signed_in_visitors_can_request(client):
    response = request_label(client)

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "not_signed_in"
    assert client.get("/api/label-requests").status_code == 401


def test_a_request_is_stored_for_the_owner_and_listed_for_the_visitor(client, fakes, db, reader):
    sign_in(client, fakes)

    response = request_label(client, "  Orbe   Records ")

    assert response.status_code == 201
    assert "waiting for review" in response.get_json()["message"]
    assert client.get("/api/label-requests").get_json() == {"requests": [{"name": "Orbe Records", "status": "waiting"}]}
    db.commit()
    assert db.execute("SELECT name, status, label_id FROM label_requests").fetchall() == [("Orbe Records", "waiting", None)]


def test_asking_twice_stores_one_request_and_costs_nothing(client, fakes, db, reader):
    sign_in(client, fakes)
    request_label(client, "Arts")

    again = request_label(client, "ARTS")

    assert again.status_code == 200 and "already asked" in again.get_json()["message"]
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (1,)


def test_equivalent_label_names_are_deduplicated(client, fakes, db, reader):
    sign_in(client, fakes)
    request_label(client, "Planet Rhythm Records")

    again = request_label(client, "planet rhythm")

    assert again.status_code == 200 and "already asked" in again.get_json()["message"]
    db.commit()
    assert db.execute("SELECT name, status FROM label_requests").fetchall() == [
        ("Planet Rhythm Records", "waiting"),
    ]


def test_notification_is_sent_and_recorded(client, fakes, db, reader):
    sign_in(client, fakes)

    response = request_label(client, "Orbe Records")

    assert response.status_code == 201
    assert ("label_request", "Orbe Records", 1) in fakes.mails
    db.commit()
    assert db.execute("SELECT notification_sent_at FROM label_requests").fetchone()[0] is not None


def test_failed_notification_is_reported_and_duplicate_submission_retries(client, fakes, db, reader):
    sign_in(client, fakes)
    fakes.mail_error = mailer.MailUnavailable("mail unavailable")

    failed = request_label(client, "Orbe Records")

    assert failed.status_code == 503
    assert "request was saved" in failed.get_json()["error"]["message"]
    db.commit()
    assert db.execute("SELECT notification_sent_at FROM label_requests").fetchone() == (None,)

    fakes.mail_error = None
    retried = request_label(client, "Orbe Records")

    assert retried.status_code == 200
    db.commit()
    assert db.execute(
        "SELECT count(*), count(*) FILTER (WHERE notification_sent_at IS NOT NULL) FROM label_requests"
    ).fetchone() == (1, 1)


@responses.activate
def test_notification_email_goes_to_the_configured_operator(monkeypatch):
    import json

    monkeypatch.setattr(config, "LABEL_REQUEST_NOTIFICATION_EMAIL", "operator@example.com")
    monkeypatch.setattr(config, "RESEND_API_KEY", "test-resend-key")
    responses.post(mailer.RESEND_URL, json={"id": "test-email"})

    mailer.send_label_request_notification("Orbe Records", 12)

    payload = json.loads(responses.calls[0].request.body)
    assert payload["to"] == ["operator@example.com"]
    assert "record label" in payload["subject"].lower()
    assert "Orbe Records" in payload["text"] and "12" in payload["text"]


def test_notification_email_requires_a_configured_operator(monkeypatch):
    monkeypatch.setattr(config, "LABEL_REQUEST_NOTIFICATION_EMAIL", "")

    with pytest.raises(mailer.MailUnavailable, match="LABEL_REQUEST_NOTIFICATION_EMAIL"):
        mailer.send_label_request_notification("Orbe Records", 12)


def test_tracked_label_lookup_failure_refuses_without_storing_request(fakes, clock, limits_db, db, monkeypatch):
    import psycopg
    from web.app import create_app

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "LABEL_REQUEST_NOTIFICATION_EMAIL", "operator@example.com")

    def broken_labels():
        raise psycopg.OperationalError("reader unavailable")

    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, mail=fakes, now=clock,
                     list_labels=broken_labels)
    api = app.test_client()
    sign_in(api, fakes)

    response = request_label(api, "Orbe Records")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "database"
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


def test_concurrent_requests_charge_quota_only_for_inserted_rows(db, limits_db, monkeypatch):
    monkeypatch.setattr(label_requests, "PER_ACCOUNT_DAY", 1)
    db.execute("INSERT INTO accounts (email, password_hash, created_at, confirmed_at) "
               "VALUES ('race@example.com', 'x', %s, %s)", (NOON, NOON))
    db.commit()
    account_id = db.execute("SELECT id FROM accounts WHERE email = 'race@example.com'").fetchone()[0]
    start = Barrier(2)

    def submit(name):
        start.wait()
        try:
            with limits.connect_limits() as conn:
                return label_requests.request_label(conn, account_id, name, NOON)[2]
        except limits.LimitReached:
            return False

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(submit, ("Label Alpha", "Label Beta")))

    assert sum(results) == 1
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests WHERE account_id = %s", (account_id,)).fetchone() == (1,)
    assert db.execute("SELECT count FROM question_counts WHERE visitor = %s AND period = 'day'",
                      (f"label-request:account:{account_id}",)).fetchone() == (1,)


def test_a_tracked_label_is_not_requested(client, fakes, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES (347, 'Planet Rhythm Records')")
    db.commit()
    sign_in(client, fakes)

    response = request_label(client, "planet rhythm")

    assert response.get_json() == {"status": "tracked",
                                   "message": "Planet Rhythm Records is already tracked. You can ask about it now."}
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


@pytest.mark.parametrize("body, code", [({"name": "x"}, "invalid_label_name"), ({"name": "Arts", "extra": 1}, "invalid_request"),
                                        ({}, "invalid_request")])
def test_bad_requests_store_nothing(client, fakes, db, body, code):
    sign_in(client, fakes)

    response = client.post("/api/label-requests", json=body)

    assert response.status_code == 400 and response.get_json()["error"]["code"] == code
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


def test_an_account_may_request_three_labels_a_day(client, fakes, reader):
    sign_in(client, fakes)

    codes = [request_label(client, f"Label {n}").status_code for n in range(4)]

    assert codes == [201, 201, 201, 429]


def test_the_whole_site_is_capped_too(client, fakes, monkeypatch, reader):
    monkeypatch.setattr(label_requests, "PER_SITE_DAY", 1)
    sign_in(client, fakes)

    assert request_label(client, "Arts").status_code == 201
    refused = request_label(client, "Orbe Records")

    assert refused.status_code == 429 and "a lot of label requests" in refused.get_json()["error"]["message"]


def test_requests_are_deleted_with_the_account(client, fakes, db, reader):
    sign_in(client, fakes)
    request_label(client, "Arts")

    client.post("/api/account/delete", json={})

    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


def test_a_visitor_sees_only_their_own_requests(client, fakes, reader):
    sign_in(client, fakes, "first@example.com")
    request_label(client, "Arts")
    client.post("/api/sign-out", json={})
    sign_in(client, fakes, "second@example.com")

    assert client.get("/api/label-requests").get_json() == {"requests": []}


# --- The owner's command -------------------------------------------------------------

@pytest.fixture
def waiting_requests(db, limits_db):
    """Two accounts asked for Orbe Records, one for Arts."""
    now = NOON
    for email in ("a@example.com", "b@example.com"):
        db.execute("INSERT INTO accounts (email, password_hash, created_at, confirmed_at) VALUES (%s, 'x', %s, %s)",
                   (email, now, now))
    db.execute("INSERT INTO label_requests (account_id, name, created_at) VALUES"
               " (1, 'Orbe Records', %s), (2, 'orbe records', %s), (1, 'Arts', %s)", (now, now, now))
    db.commit()


@pytest.fixture
def owner_db(db, postgres_url, monkeypatch):
    """ingestion.database.connect() pointed at the test database."""
    import psycopg
    from ingestion import database
    monkeypatch.setattr(database, "connect", lambda: psycopg.connect(postgres_url))


def test_the_owner_sees_waiting_requests_by_name(waiting_requests, owner_db, capsys):
    assert owner.main(["list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "Orbe Records  (2 accounts" in lines[0] and "Arts  (1 account," in lines[1]


def test_the_owner_groups_and_decides_equivalent_names(waiting_requests, owner_db, db, capsys):
    db.execute("INSERT INTO accounts (email, password_hash, created_at, confirmed_at) VALUES "
               "('c@example.com', 'x', %s, %s), ('d@example.com', 'x', %s, %s)", (NOON, NOON, NOON, NOON))
    db.execute("INSERT INTO label_requests (account_id, name, created_at) VALUES "
               "(3, 'Planet Rhythm Records', %s), (4, 'planet rhythm (2)', %s), (3, 'Planet Rhythm', %s)",
               (NOON, NOON, NOON))
    db.commit()
    request_id = db.execute("SELECT min(id) FROM label_requests WHERE name = 'Planet Rhythm Records'").fetchone()[0]

    assert owner.main(["list"]) == 0
    assert "Planet Rhythm Records  (2 accounts" in capsys.readouterr().out
    assert owner.main(["decline", str(request_id)]) == 0
    assert "Declined 3 request(s)" in capsys.readouterr().out
    db.rollback()
    assert db.execute("SELECT count(*) FROM label_requests WHERE name ILIKE 'planet rhythm%' AND status = 'declined'").fetchone() == (3,)


def test_notification_command_retries_pending_emails(waiting_requests, owner_db, db, monkeypatch, capsys):
    sent = []
    monkeypatch.setattr(owner.mailer, "send_label_request_notification",
                        lambda name, request_id: sent.append((name, request_id)))

    assert owner.main(["notify"]) == 0

    assert len(sent) == 3
    assert "Sent 3 notification(s); 0 still pending." in capsys.readouterr().out
    db.rollback()
    assert db.execute("SELECT count(*) FROM label_requests WHERE notification_sent_at IS NOT NULL").fetchone() == (3,)


def test_notification_command_keeps_failures_pending(waiting_requests, owner_db, db, monkeypatch, capsys):
    def fail(_name, _request_id):
        raise mailer.MailUnavailable("email is not configured")

    monkeypatch.setattr(owner.mailer, "send_label_request_notification", fail)

    assert owner.main(["notify"]) == 1

    assert "still pending" in capsys.readouterr().out
    db.rollback()
    assert db.execute("SELECT count(*) FROM label_requests WHERE notification_sent_at IS NULL").fetchone() == (3,)


@responses.activate
def test_searching_shows_matching_labels_with_their_size(waiting_requests, owner_db, capsys):
    responses.get(f"{DISCOGS}/database/search", json={"results": [{"id": 698526, "title": "Orbe Records"},
                                                                 {"id": 120424, "title": "Orbe"}]})
    responses.get(f"{DISCOGS}/labels/698526/releases", json={"pagination": {"items": 26, "pages": 26}, "releases": []})
    responses.get(f"{DISCOGS}/labels/120424/releases", json={"pagination": {"items": 1171, "pages": 1171}, "releases": []})

    assert owner.main(["search", "1"]) == 0

    out = capsys.readouterr().out
    assert "698526  Orbe Records  (26 entries)" in out and "120424  Orbe  (1171 entries)" in out
    assert responses.calls[0].request.params == {"type": "label", "q": "Orbe Records", "per_page": "5"}


@responses.activate
def test_approving_tracks_the_label_and_answers_everyone_who_asked(waiting_requests, owner_db, db, capsys):
    responses.get(f"{DISCOGS}/labels/698526", json={"id": 698526, "name": "Orbe Records"})

    assert owner.main(["approve", "1", "698526"]) == 0

    assert "Approved 2 request(s) for Orbe Records" in capsys.readouterr().out
    db.rollback()
    assert [(row[0], row[1], row[3]) for row in tracked_labels(db)] == [(698526, "Orbe Records", "request")]
    assert db.execute("SELECT name, status, label_id FROM label_requests ORDER BY id").fetchall() == [
        ("Orbe Records", "added", 698526), ("orbe records", "added", 698526), ("Arts", "waiting", None)]


def test_declining_answers_everyone_who_asked_for_that_name(waiting_requests, owner_db, db):
    assert owner.main(["decline", "3"]) == 0

    db.rollback()
    assert db.execute("SELECT status FROM label_requests WHERE name = 'Arts'").fetchone() == ("declined",)
    assert db.execute("SELECT count(*) FROM label_requests WHERE status = 'waiting'").fetchone() == (2,)


@responses.activate
def test_a_label_missing_on_discogs_changes_nothing(waiting_requests, owner_db, db, capsys):
    responses.get(f"{DISCOGS}/labels/999999999", status=404)

    assert owner.main(["approve", "1", "999999999"]) == 1

    assert "not on Discogs" in capsys.readouterr().err
    db.rollback()
    assert db.execute("SELECT count(*) FROM label_requests WHERE status = 'waiting'").fetchone() == (3,)


@pytest.mark.parametrize("argv", [[], ["approve", "1"], ["search"], ["decline", "x"], ["delete", "1"]])
def test_wrong_use_shows_how(argv, capsys):
    assert owner.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


def test_an_unknown_request_is_reported(waiting_requests, owner_db, capsys):
    assert owner.main(["decline", "99"]) == 1
    assert "no request 99" in capsys.readouterr().err
