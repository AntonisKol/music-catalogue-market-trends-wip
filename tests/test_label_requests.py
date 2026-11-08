"""Label requests: signed-in visitors ask for labels, the owner reviews and decides.

The web API never calls Discogs; the owner's command does, and Discogs is faked
here with responses. Emails are kept by a fake sender. Everything else runs
against the real test database.
"""
import psycopg
import pytest
import responses

from ingestion import discogs_client, label_requests as owner
from ingestion.track_labels import tracked_labels
from tests.test_sign_in import NOON, Clock, Fakes, ask, clock, client, fakes, sign_in  # noqa: F401 (fixtures)
from web import accounts, config, label_requests

DISCOGS = discogs_client.BASE_URL


@pytest.fixture(autouse=True)
def tracked_labels_readable(reader):
    """The API reads the tracked labels before storing a request, from the test database."""


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


def test_a_request_is_stored_for_the_owner_and_listed_for_the_visitor(client, fakes, db):
    sign_in(client, fakes)

    response = request_label(client, "  Orbe   Records ")

    assert response.status_code == 201
    assert "waiting for review" in response.get_json()["message"]
    assert client.get("/api/label-requests").get_json() == {"requests": [{"name": "Orbe Records", "status": "waiting", "reason": None}]}
    db.commit()
    assert db.execute("SELECT name, status, label_id FROM label_requests").fetchall() == [("Orbe Records", "waiting", None)]


def test_asking_twice_stores_one_request_and_costs_nothing(client, fakes, db):
    sign_in(client, fakes)
    request_label(client, "Arts")

    again = request_label(client, "ARTS")

    assert again.status_code == 200 and "already asked" in again.get_json()["message"]
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (1,)


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


def test_an_account_may_request_three_labels_a_day(client, fakes):
    sign_in(client, fakes)

    codes = [request_label(client, f"Label {n}").status_code for n in range(4)]

    assert codes == [201, 201, 201, 429]


def test_the_whole_site_is_capped_too(client, fakes, monkeypatch):
    monkeypatch.setattr(label_requests, "PER_SITE_DAY", 1)
    sign_in(client, fakes)

    assert request_label(client, "Arts").status_code == 201
    refused = request_label(client, "Orbe Records")

    assert refused.status_code == 429 and "a lot of label requests" in refused.get_json()["error"]["message"]


def test_requests_are_deleted_with_the_account(client, fakes, db):
    sign_in(client, fakes)
    request_label(client, "Arts")

    client.post("/api/account/delete", json={})

    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


def test_a_visitor_sees_only_their_own_requests(client, fakes):
    sign_in(client, fakes, "first@example.com")
    request_label(client, "Arts")
    client.post("/api/sign-out", json={})
    sign_in(client, fakes, "second@example.com")

    assert client.get("/api/label-requests").get_json() == {"requests": []}


def test_an_equivalent_name_counts_as_the_same_request(client, fakes, db):
    sign_in(client, fakes)
    request_label(client, "Polegroup")

    again = request_label(client, "polegroup records")

    assert again.status_code == 200 and "already asked" in again.get_json()["message"]
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (1,)


def test_a_request_is_refused_when_the_tracked_labels_cannot_be_read(fakes, clock, limits_db, monkeypatch, db):
    from web.app import create_app
    from tests.test_sign_in import sign_in as signed_in

    def broken():
        raise psycopg.OperationalError("connection refused")

    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, mail=fakes, now=clock, list_labels=broken)
    client = app.test_client()
    signed_in(client, fakes)

    response = request_label(client, "Arts")

    assert response.status_code == 503 and response.get_json()["error"]["code"] == "database"
    db.commit()
    assert db.execute("SELECT count(*) FROM label_requests").fetchone() == (0,)


def test_a_rejected_request_shows_the_owners_reason(client, fakes, db):
    sign_in(client, fakes)
    request_label(client, "Arts")
    db.execute("UPDATE label_requests SET status = 'rejected', reason = 'Not a techno label.'")
    db.commit()

    assert client.get("/api/label-requests").get_json() == {
        "requests": [{"name": "Arts", "status": "rejected", "reason": "Not a techno label."}]}


# --- The owner's command -------------------------------------------------------------

@pytest.fixture
def waiting_requests(db, limits_db):
    """Two accounts asked for Pole Recordings, under different names; one asked for Arts."""
    for email in ("a@example.com", "b@example.com"):
        db.execute("INSERT INTO accounts (email, password_hash, created_at, confirmed_at) VALUES (%s, 'x', %s, %s)",
                   (email, NOON, NOON))
    db.execute("INSERT INTO label_requests (account_id, name, created_at) VALUES"
               " (1, 'Polegroup', %s), (2, 'polegroup records', %s), (1, 'Arts', %s)", (NOON, NOON, NOON))
    db.commit()


@pytest.fixture
def owner_db(db, postgres_url, monkeypatch):
    """ingestion.database.connect() pointed at the test database."""
    from ingestion import database
    monkeypatch.setattr(database, "connect", lambda: psycopg.connect(postgres_url))


class Outbox:
    """Emails the command sends, kept instead of sent."""

    def __init__(self):
        self.sent = []
        self.fail = False

    def keep(self, kind):
        def send(*args):
            if self.fail:
                raise owner.mailer.MailUnavailable("Resend did not accept the email (status 500)")
            self.sent.append((kind, *args))
        return send


@pytest.fixture
def outbox(monkeypatch):
    box = Outbox()
    monkeypatch.setattr(owner.mailer, "send_label_request", box.keep("request"))
    monkeypatch.setattr(owner.mailer, "send_label_status",
                        lambda email, name, status, reason=None, label=None: box.keep(status)(email, name, reason, label))
    monkeypatch.setattr(owner.config, "LABEL_REQUEST_EMAIL", "reviewer@example.com")
    return box


def statuses(db):
    db.rollback()
    return db.execute("SELECT name, status, label_id, reason FROM label_requests ORDER BY id").fetchall()


def discogs_matches():
    responses.get(f"{DISCOGS}/database/search", json={"results": [{"id": 21250, "title": "Pole Recordings"},
                                                                 {"id": 922804, "title": "PoleGroup Radio"}]})
    responses.get(f"{DISCOGS}/labels/21250/releases", json={"pagination": {"items": 226, "pages": 226}, "releases": []})
    responses.get(f"{DISCOGS}/labels/922804/releases", json={"pagination": {"items": 6, "pages": 6}, "releases": []})


def test_the_owner_sees_open_requests_with_equivalent_names_together(waiting_requests, owner_db, capsys):
    assert owner.main(["list"]) == 0

    lines = capsys.readouterr().out.splitlines()
    assert "Polegroup  (2 accounts, since 2026-11-01)" in lines[0] and "Arts  (1 account," in lines[1]


@responses.activate
@pytest.mark.parametrize("label", ["21250", "https://www.discogs.com/label/21250-Pole-Recordings"])
def test_approving_tracks_the_label_for_everyone_who_asked(waiting_requests, owner_db, db, outbox, capsys, label):
    responses.get(f"{DISCOGS}/labels/21250", json={"id": 21250, "name": "Pole Recordings"})

    assert owner.main(["approve", "1", label]) == 0

    assert "Approved 2 request(s) for Polegroup; everyone who asked was emailed." in capsys.readouterr().out
    assert sorted(outbox.sent) == [("added", "a@example.com", "Polegroup", None, "Pole Recordings"),
                                   ("added", "b@example.com", "polegroup records", None, "Pole Recordings")]
    assert statuses(db)[:2] == [("Polegroup", "added", 21250, None), ("polegroup records", "added", 21250, None)]
    assert [(row[0], row[1], row[3]) for row in tracked_labels(db)] == [(21250, "Pole Recordings", "request")]


def test_rejecting_emails_the_reason_to_everyone_who_asked(waiting_requests, owner_db, db, outbox):
    assert owner.main(["reject", "2", "Not", "a", "techno", "label."]) == 0

    assert statuses(db)[:2] == [("Polegroup", "rejected", None, "Not a techno label."),
                                ("polegroup records", "rejected", None, "Not a techno label.")]
    assert sorted(outbox.sent) == [("rejected", "a@example.com", "Polegroup", "Not a techno label.", None),
                                   ("rejected", "b@example.com", "polegroup records", "Not a techno label.", None)]
    assert db.execute("SELECT count(*) FROM label_requests WHERE visitor_notified_at IS NOT NULL").fetchone() == (2,)


def test_a_failed_rejection_email_is_retried_later(waiting_requests, owner_db, db, outbox):
    outbox.fail = True
    owner.main(["reject", "3", "Already covered by another label."])
    assert outbox.sent == []

    outbox.fail = False
    assert owner.main(["notify"]) == 0
    owner.main(["notify"])

    assert [mail[:2] for mail in outbox.sent if mail[0] == "rejected"] == [("rejected", "a@example.com")]


def test_a_decision_is_emailed_once(waiting_requests, owner_db, db, outbox, monkeypatch):
    monkeypatch.setattr(owner, "matches_on_discogs", lambda name: [])
    owner.main(["reject", "3", "Already covered by another label."])
    owner.main(["notify"])
    owner.main(["notify"])

    assert [(mail[0], mail[1]) for mail in outbox.sent if mail[0] != "request"] == [("rejected", "a@example.com")]


def test_there_is_no_review_step(waiting_requests, owner_db, db, capsys):
    assert owner.main(["review", "3"]) == 2

    assert "review" not in capsys.readouterr().err
    assert statuses(db)[2][1] == "waiting"


@pytest.mark.parametrize("reason", ["no", "x" * 501])
def test_a_rejection_needs_a_real_reason(waiting_requests, owner_db, db, outbox, reason):
    assert owner.main(["reject", "3", reason]) == 2

    assert statuses(db)[2][1] == "waiting" and outbox.sent == []


@responses.activate
def test_new_requests_are_emailed_to_the_reviewer_once(waiting_requests, owner_db, db, outbox):
    discogs_matches()
    responses.get(f"{DISCOGS}/database/search", json={"results": []})

    assert owner.main(["notify"]) == 0
    owner.main(["notify"])

    reviewer = [mail for mail in outbox.sent if mail[0] == "request"]
    assert [(m[1], m[2], m[3], m[4]) for m in reviewer] == [
        ("reviewer@example.com", 1, "Polegroup", 2), ("reviewer@example.com", 3, "Arts", 1)]
    assert reviewer[0][5] == [(21250, "Pole Recordings", 226), (922804, "PoleGroup Radio", 6)]


@responses.activate
def test_a_request_is_still_emailed_when_discogs_is_down(waiting_requests, owner_db, outbox, capsys):
    responses.get(f"{DISCOGS}/database/search", status=500)

    owner.main(["notify"])

    assert [(mail[3], mail[5]) for mail in outbox.sent] == [("Polegroup", []), ("Arts", [])]


def test_without_a_reviewer_address_nothing_is_emailed(waiting_requests, owner_db, outbox, monkeypatch):
    monkeypatch.setattr(owner.config, "LABEL_REQUEST_EMAIL", "")

    assert owner.main(["notify"]) == 0
    assert outbox.sent == []


@responses.activate
def test_a_label_missing_on_discogs_changes_nothing(waiting_requests, owner_db, db, capsys):
    responses.get(f"{DISCOGS}/labels/999999999", status=404)

    assert owner.main(["approve", "1", "999999999"]) == 1

    assert "not on Discogs" in capsys.readouterr().err
    assert [row[1] for row in statuses(db)] == ["waiting", "waiting", "waiting"]


@pytest.mark.parametrize("text, expected", [("21250", 21250), ("https://www.discogs.com/label/21250-Pole-Recordings", 21250),
                                            ("https://www.discogs.com/de/label/347-Planet-Rhythm-Records", 347)])
def test_a_label_can_be_given_by_id_or_link(text, expected):
    assert owner.discogs_label_id(text) == expected


def test_a_link_that_is_not_a_label_is_refused(waiting_requests, owner_db, capsys):
    assert owner.main(["approve", "1", "https://www.discogs.com/artist/123-Someone"]) == 2
    assert "not a Discogs label id or link" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["approve", "1"], ["review"], ["reject", "1"], ["review", "x"], ["delete", "1"],
                                  ["list", "1"]])
def test_wrong_use_shows_how(argv, capsys):
    assert owner.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


def test_a_decided_request_cannot_be_decided_again(waiting_requests, owner_db, db, outbox, capsys):
    owner.main(["reject", "3", "Not a techno label."])

    assert owner.main(["reject", "3", "Another reason."]) == 1
    assert "not waiting for review" in capsys.readouterr().err


def test_requests_decided_before_status_emails_are_not_emailed_again(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.execute("ALTER TABLE label_requests DROP COLUMN visitor_notified_status")
    db.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'x', now())")
    db.execute("INSERT INTO label_requests (account_id, name, status, created_at) VALUES"
               " (1, 'Arts', 'added', now()), (1, 'Wax', 'waiting', now())")
    db.commit()

    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()
    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT name, visitor_notified_status FROM label_requests ORDER BY id").fetchall() == [
        ("Arts", "added"), ("Wax", None)]
    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.commit()


def test_requests_being_reviewed_wait_for_review_again(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.execute("ALTER TABLE label_requests DROP CONSTRAINT label_requests_status_check")
    db.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'x', now())")
    db.execute("INSERT INTO label_requests (account_id, name, status, visitor_notified_status, created_at)"
               " VALUES (1, 'Arts', 'reviewing', 'reviewing', now())")
    db.commit()

    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT status, visitor_notified_status FROM label_requests").fetchone() == ("waiting", None)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE label_requests SET status = 'reviewing'")
    db.rollback()
    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.commit()


def test_old_declined_requests_become_rejected(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.execute("ALTER TABLE label_requests DROP CONSTRAINT label_requests_status_check")
    db.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'x', now())")
    db.execute("INSERT INTO label_requests (account_id, name, status, created_at) VALUES (1, 'Arts', 'declined', now())")
    db.commit()

    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT status FROM label_requests").fetchone() == ("rejected",)
    with pytest.raises(psycopg.errors.CheckViolation):
        db.execute("UPDATE label_requests SET status = 'declined'")
    db.rollback()
    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.commit()
