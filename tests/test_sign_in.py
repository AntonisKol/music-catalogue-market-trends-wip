"""Email sign-in: one-time links, sessions, account quotas and deletion.

Links are never really emailed: the app is given a fake sender that keeps them.
Everything else runs against the real test database through the web login.
"""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import pytest
import requests
import responses

from web import accounts, config, mailer
from web.app import CONFIRM_COOKIE, SESSION_COOKIE, create_app
from web.limits import connect_limits

NOON = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)
EMAIL = "listener@example.com"


# --- accounts.py on its own ------------------------------------------------------

@pytest.fixture
def conn(limits_db):
    with connect_limits() as conn:
        yield conn


@pytest.mark.parametrize("text, expected", [
    ("  Listener@Example.COM ", "listener@example.com"),
    ("a.b+tag@sub.example.co.uk", "a.b+tag@sub.example.co.uk"),
])
def test_addresses_are_stored_lower_cased(text, expected):
    assert accounts.normalize_email(text) == expected


@pytest.mark.parametrize("text", ["", "listener", "listener@example", "a b@example.com", "a@@example.com",
                                  "x" * 250 + "@example.com"])
def test_what_cannot_be_an_address_is_refused(text):
    with pytest.raises(ValueError):
        accounts.normalize_email(text)


def test_a_link_works_once(conn):
    token = accounts.create_sign_in_link(conn, EMAIL, NOON)

    session, account_id, email = accounts.sign_in(conn, token, NOON + timedelta(minutes=1))
    assert email == EMAIL and session and account_id
    assert accounts.sign_in(conn, token, NOON + timedelta(minutes=2)) is None
    assert accounts.link_email(conn, token, NOON + timedelta(minutes=2)) is None


def test_a_link_expires_after_15_minutes(conn):
    token = accounts.create_sign_in_link(conn, EMAIL, NOON)

    assert accounts.link_email(conn, token, NOON + timedelta(minutes=14, seconds=59)) == EMAIL
    assert accounts.sign_in(conn, token, NOON + timedelta(minutes=15)) is None


def test_looking_at_a_link_does_not_use_it_up(conn):
    token = accounts.create_sign_in_link(conn, EMAIL, NOON)

    assert accounts.link_email(conn, token, NOON) == EMAIL
    assert accounts.link_email(conn, token, NOON) == EMAIL
    assert accounts.sign_in(conn, token, NOON) is not None


def test_an_unknown_token_signs_nobody_in(conn):
    accounts.create_sign_in_link(conn, EMAIL, NOON)

    assert accounts.sign_in(conn, "made-up-token", NOON) is None


def test_tokens_are_stored_only_as_hashes(conn, db):
    token = accounts.create_sign_in_link(conn, EMAIL, NOON)
    session, _, _ = accounts.sign_in(conn, token, NOON)

    db.commit()
    stored = str(db.execute("SELECT * FROM sign_in_tokens").fetchall() + db.execute("SELECT * FROM sessions").fetchall())
    assert token not in stored and session not in stored
    assert accounts.hashed(token) in stored and accounts.hashed(session) in stored


def test_signing_in_again_uses_the_same_account(conn):
    first = accounts.sign_in(conn, accounts.create_sign_in_link(conn, EMAIL, NOON), NOON)
    tomorrow = NOON + timedelta(days=1)
    second = accounts.sign_in(conn, accounts.create_sign_in_link(conn, EMAIL, tomorrow), tomorrow)

    assert first[1] == second[1]
    assert first[0] != second[0]  # a new session each time


def test_sessions_last_30_days_and_end_on_sign_out(conn):
    session, account_id, _ = accounts.sign_in(conn, accounts.create_sign_in_link(conn, EMAIL, NOON), NOON)

    assert accounts.session_account(conn, session, NOON + timedelta(days=29)) == (account_id, EMAIL)
    assert accounts.session_account(conn, session, NOON + timedelta(days=30)) is None
    accounts.sign_out(conn, session)
    assert accounts.session_account(conn, session, NOON) is None


def test_deleting_an_account_removes_everything_stored_for_it(conn, db):
    session, account_id, _ = accounts.sign_in(conn, accounts.create_sign_in_link(conn, EMAIL, NOON), NOON)
    accounts.create_sign_in_link(conn, EMAIL, NOON)  # an unused link
    conn.execute("INSERT INTO question_counts VALUES (%s, 'day', %s, 4)", (f"account:{account_id}", NOON))

    accounts.delete_account(conn, account_id, f"account:{account_id}")

    db.commit()
    for table in ("accounts", "sessions", "sign_in_tokens", "question_counts"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table


# --- Sending the email --------------------------------------------------------------

@responses.activate
def test_the_link_is_sent_through_resend(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    responses.post(mailer.RESEND_URL, json={"id": "1"})

    mailer.send_sign_in_link(EMAIL, "https://api.example/api/sign-in/confirm?token=abc")

    request = responses.calls[0].request
    assert request.headers["Authorization"] == "Bearer re_test"
    sent = request.body.decode()
    assert EMAIL in sent and "token=abc" in sent and config.SIGN_IN_EMAIL_FROM in sent


@responses.activate
@pytest.mark.parametrize("reply", [{"status": 422, "json": {}}, {"body": requests.ConnectionError("down")}])
def test_a_refused_or_failed_send_is_reported(monkeypatch, reply):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    responses.post(mailer.RESEND_URL, **reply)

    with pytest.raises(mailer.MailUnavailable):
        mailer.send_sign_in_link(EMAIL, "https://x/?token=abc")


def test_without_a_key_sign_in_is_off_unless_links_go_to_the_log(monkeypatch, caplog):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", False)
    with pytest.raises(mailer.MailUnavailable):
        mailer.send_sign_in_link(EMAIL, "https://x/?token=abc")

    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", True)
    mailer.send_sign_in_link(EMAIL, "https://x/?token=abc")
    assert "token=abc" in caplog.text


# --- Through the web API ----------------------------------------------------------

class Fakes:
    def __init__(self):
        self.links = []
        self.questions = []
        self.tokens = []
        self.human = True
        self.mail_error = None

    def answer(self, question):
        from queries.questions import Answer
        self.questions.append(question)
        return Answer(text="An answer.", model="claude-haiku-5-5")

    def verify(self, token, secret, remote_ip):
        self.tokens.append(token)
        return self.human

    def send_link(self, email, link):
        if self.mail_error:
            raise self.mail_error
        self.links.append((email, link))


class Clock:
    def __init__(self):
        self.now = NOON

    def __call__(self):
        return self.now


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def client(fakes, clock, limits_db, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    # The test client talks plain http, which a Secure cookie would not be sent over.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    monkeypatch.setattr(config, "PUBLIC_API_URL", "https://api.example")
    monkeypatch.setattr(config, "SIGN_IN_RETURN_URL", "https://site.example/market-trends/")
    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, send_link=fakes.send_link, now=clock)
    return app.test_client()


def ask_for_link(client, email=EMAIL, ip="203.0.113.7"):
    return client.post("/api/sign-in", json={"email": email, "captcha_token": "good"},
                       environ_base={"REMOTE_ADDR": ip})


def token_in(link):
    return parse_qs(urlsplit(link).query)["token"][0]


def sign_in(client, fakes, email=EMAIL):
    assert ask_for_link(client, email).status_code == 202
    token = token_in(fakes.links[-1][1])
    page = client.get(f"/api/sign-in/confirm?token={token}")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    return client.post("/api/sign-in/confirm", data={"token": token, "nonce": nonce})


def ask(client, ip="203.0.113.7", token="good"):
    return client.post("/api/questions", json={"question": "Cheapest?", "captcha_token": token},
                       environ_base={"REMOTE_ADDR": ip})


def test_asking_for_a_link_emails_one(client, fakes):
    response = ask_for_link(client, " Listener@Example.com ")

    assert response.status_code == 202
    assert response.get_json()["status"] == "sent"
    (email, link), = fakes.links
    assert email == EMAIL
    assert link.startswith("https://api.example/api/sign-in/confirm?token=")
    assert fakes.tokens == ["good"]


def test_the_answer_is_the_same_whether_or_not_the_address_has_an_account(client, fakes):
    sign_in(client, fakes)

    known = ask_for_link(client, EMAIL, ip="198.51.100.1")
    unknown = ask_for_link(client, "someone-else@example.com", ip="198.51.100.2")

    assert (known.status_code, known.get_json()) == (unknown.status_code, unknown.get_json())


@pytest.mark.parametrize("body, code", [
    ({"email": "not-an-address", "captcha_token": "good"}, "invalid_email"),
    ({"email": EMAIL}, "invalid_request"),
    ({"email": EMAIL, "captcha_token": "good", "admin": True}, "invalid_request"),
])
def test_bad_sign_in_requests_send_nothing(client, fakes, body, code):
    response = client.post("/api/sign-in", json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code
    assert fakes.links == [] and fakes.tokens == []


def test_a_failed_human_check_sends_no_link(client, fakes):
    fakes.human = False

    assert ask_for_link(client).status_code == 403
    assert fakes.links == []


def test_links_are_limited_per_address(client, fakes):
    statuses = [ask_for_link(client, ip=f"198.51.100.{n}").status_code for n in range(4)]

    assert statuses == [202, 202, 202, 429]
    assert len(fakes.links) == 3


def test_links_are_limited_per_visitor(client, fakes):
    statuses = [ask_for_link(client, f"person{n}@example.com").status_code for n in range(6)]

    assert statuses == [202] * 5 + [429]
    assert ask_for_link(client).get_json()["error"]["code"] == "sign_in_limit_reached"


def test_without_email_set_up_sign_in_says_so(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", False)

    response = ask_for_link(client)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "sign_in_unavailable"
    assert fakes.tokens == []


def test_a_send_failure_is_reported(client, fakes):
    fakes.mail_error = mailer.MailUnavailable("status 500")

    response = ask_for_link(client)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "sign_in_unavailable"


def test_opening_the_link_shows_a_confirm_page_and_signs_nobody_in(client, fakes):
    ask_for_link(client)
    token = token_in(fakes.links[0][1])

    page = client.get(f"/api/sign-in/confirm?token={token}")
    again = client.get(f"/api/sign-in/confirm?token={token}")  # a mail scanner, then the person

    assert page.status_code == again.status_code == 200
    assert page.mimetype == "text/html"
    assert EMAIL in page.get_data(as_text=True) and "<button" in page.get_data(as_text=True)
    assert client.get("/api/account").get_json()["signed_in"] is False


def test_the_confirm_page_escapes_the_address(client, fakes):
    ask_for_link(client, '"><script>x</script>@example.com')
    token = token_in(fakes.links[0][1])

    html = client.get(f"/api/sign-in/confirm?token={token}").get_data(as_text=True)

    assert "<script>x" not in html and "&lt;script&gt;" in html


def test_the_confirm_page_cannot_be_framed_or_run_scripts(client, fakes):
    ask_for_link(client)
    response = client.get(f"/api/sign-in/confirm?token={token_in(fakes.links[0][1])}")

    policy = response.headers["Content-Security-Policy"]
    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy
    assert "form-action 'self' https://site.example" in policy
    assert response.headers["Referrer-Policy"] == "no-referrer"


def test_pressing_sign_in_starts_a_session_and_returns_to_the_site(client, fakes):
    response = sign_in(client, fakes)

    assert response.status_code == 303
    assert response.headers["Location"] == "https://site.example/market-trends/"
    cookie = next(c for c in response.headers.getlist("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/api" in cookie
    assert client.get("/api/account").get_json() == {
        "signed_in": True, "email": EMAIL,
        "limits": {"hour": config.QUESTIONS_PER_ACCOUNT_HOUR, "day": config.QUESTIONS_PER_ACCOUNT_DAY},
        "remaining": {"hour": config.QUESTIONS_PER_ACCOUNT_HOUR, "day": config.QUESTIONS_PER_ACCOUNT_DAY},
    }


def test_the_session_cookie_is_https_only_by_default(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)

    response = sign_in(client, fakes)

    cookie = next(c for c in response.headers.getlist("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))
    assert "Secure" in cookie


def test_a_confirm_posted_from_another_site_is_refused(client, fakes):
    # Another site can post the form, but cannot send the cookie the confirm page set.
    ask_for_link(client)
    token = token_in(fakes.links[0][1])

    response = client.post("/api/sign-in/confirm", data={"token": token, "nonce": "guessed"})

    assert response.status_code == 403
    assert client.get("/api/account").get_json()["signed_in"] is False
    assert sign_in_with(client, token).status_code == 303  # the link still works


def sign_in_with(client, token):
    page = client.get(f"/api/sign-in/confirm?token={token}")
    nonce = re.search(r'name="nonce" value="([^"]+)"', page.get_data(as_text=True)).group(1)
    return client.post("/api/sign-in/confirm", data={"token": token, "nonce": nonce})


def test_a_used_or_expired_link_shows_a_page_to_ask_again(client, fakes, clock):
    sign_in(client, fakes)
    used = token_in(fakes.links[0][1])
    ask_for_link(client)
    expired = token_in(fakes.links[1][1])
    clock.now = NOON + timedelta(minutes=16)

    for token in (used, expired, "made-up"):
        response = client.get(f"/api/sign-in/confirm?token={token}")
        assert response.status_code == 400
        assert "https://site.example/market-trends/" in response.get_data(as_text=True)


def test_signed_in_questions_skip_the_human_check_and_count_per_account(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_ACCOUNT_HOUR", 2)
    sign_in(client, fakes)
    fakes.tokens.clear()

    responses_ = [ask(client, token="") for _ in range(3)]

    assert [r.status_code for r in responses_] == [200, 200, 429]
    assert responses_[0].get_json()["remaining"] == {"hour": 1, "day": config.QUESTIONS_PER_ACCOUNT_DAY - 1}
    assert responses_[2].get_json()["error"]["code"] == "account_limit_reached"
    assert fakes.tokens == []


def test_an_account_is_not_limited_by_its_address(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_DAY", 1)
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429

    sign_in(client, fakes)

    assert ask(client).status_code == 200


def test_the_site_cap_applies_to_accounts_too(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_SITE_DAY", 1)
    assert ask(client, ip="198.51.100.9").status_code == 200

    sign_in(client, fakes)

    assert ask(client).get_json()["error"]["code"] == "site_limit_reached"


def test_anonymous_visitors_see_what_is_left(client):
    ask(client, ip="203.0.113.7")

    assert client.get("/api/account", environ_base={"REMOTE_ADDR": "203.0.113.7"}).get_json() == {
        "signed_in": False, "email": None,
        "limits": {"hour": config.QUESTIONS_PER_VISITOR_HOUR, "day": config.QUESTIONS_PER_VISITOR_DAY},
        "remaining": {"hour": config.QUESTIONS_PER_VISITOR_HOUR - 1, "day": config.QUESTIONS_PER_VISITOR_DAY - 1},
    }


def test_signing_out_ends_the_session(client, fakes):
    sign_in(client, fakes)
    session = client.get_cookie(SESSION_COOKIE, path="/api").value

    response = client.post("/api/sign-out", json={})

    assert response.status_code == 200
    assert client.get("/api/account").get_json()["signed_in"] is False
    client.set_cookie(SESSION_COOKIE, session, path="/api")  # a copy of the old cookie
    assert client.get("/api/account").get_json()["signed_in"] is False


@pytest.mark.parametrize("path", ["/api/sign-out", "/api/account/delete"])
def test_sign_out_and_delete_need_a_json_request(client, fakes, path):
    sign_in(client, fakes)

    response = client.post(path, data="x", content_type="text/plain")

    assert response.status_code == 400
    assert client.get("/api/account").get_json()["signed_in"] is True


def test_deleting_the_account_signs_out_and_removes_it(client, fakes, db):
    sign_in(client, fakes)
    ask(client)

    response = client.post("/api/account/delete", json={})

    assert response.status_code == 200
    assert client.get("/api/account").get_json()["signed_in"] is False
    db.commit()
    assert db.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
    assert db.execute("SELECT count(*) FROM question_counts WHERE visitor LIKE 'account:%'").fetchone() == (0,)


def test_deleting_needs_a_session(client):
    response = client.post("/api/account/delete", json={})

    assert response.status_code == 401
    assert response.get_json()["error"]["code"] == "not_signed_in"


def test_the_confirm_cookie_is_cleared_after_signing_in(client, fakes):
    response = sign_in(client, fakes)

    cleared = next(c for c in response.headers.getlist("Set-Cookie") if c.startswith(CONFIRM_COOKIE + "="))
    assert "Max-Age=0" in cleared or "expires=Thu, 01 Jan 1970" in cleared


def test_when_the_hour_and_the_day_are_both_used_up_the_day_is_reported(client, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 2)
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_DAY", 2)
    ask(client)
    ask(client)

    response = ask(client)

    assert "a day" in response.get_json()["error"]["message"]
    assert response.headers["Retry-After"] == str(11 * 3600 + 30 * 60 + 1)  # until midnight UTC


def test_signed_in_questions_count_against_the_account_not_the_address(client, fakes, db):
    sign_in(client, fakes)
    ask(client)
    ask(client)
    client.post("/api/sign-out", json={})

    anonymous = client.get("/api/account", environ_base={"REMOTE_ADDR": "203.0.113.7"}).get_json()

    assert anonymous["remaining"] == {"hour": config.QUESTIONS_PER_VISITOR_HOUR, "day": config.QUESTIONS_PER_VISITOR_DAY}
    db.commit()
    questions = db.execute(
        "SELECT visitor, count FROM question_counts"
        " WHERE period = 'day' AND visitor <> 'site' AND visitor NOT LIKE 'sign-in-%'"
    ).fetchall()
    assert questions == [("account:1", 2)]


def test_setting_up_the_login_replaces_accounts_from_when_sign_in_used_passwords(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("DROP TABLE sessions")
    db.execute("DROP TABLE accounts")
    db.execute("""CREATE TABLE accounts (id BIGSERIAL PRIMARY KEY, username TEXT NOT NULL UNIQUE,
                  password_hash TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL, last_sign_in_at TIMESTAMPTZ)""")
    db.execute("""CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, account_id BIGINT NOT NULL REFERENCES accounts(id),
                  created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL)""")
    db.execute("INSERT INTO accounts (username, password_hash, created_at) VALUES ('owner', 'hash', now())")
    db.execute("""INSERT INTO question_counts VALUES ('account:1', 'day', now(), 1), ('sign-in-user:x', 'day', now(), 1),
                  ('some-visitor', 'day', now(), 1)""")

    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
    left = [visitor for (visitor,) in db.execute("SELECT visitor FROM question_counts").fetchall()]
    assert "some-visitor" in left and "account:1" not in left and "sign-in-user:x" not in left
    db.execute("INSERT INTO accounts (email, created_at, last_sign_in_at) VALUES ('a@example.com', now(), now())")
    db.rollback()


def test_setting_up_the_login_again_keeps_email_accounts(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("INSERT INTO accounts (email, created_at, last_sign_in_at) VALUES ('a@example.com', now(), now())")
    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT email FROM accounts").fetchall() == [("a@example.com",)]
