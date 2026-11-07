"""Signing up: accounts, confirmation links, passwords, sessions, quotas and deletion.

Emails are never really sent: the app is given a fake sender that keeps them.
Everything else runs against the real test database through the web login.
"""
import base64
import json
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
PASSWORD = "a long enough password"
NEW_PASSWORD = "another long password"


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


def test_passwords_must_be_long_enough():
    with pytest.raises(ValueError):
        accounts.check_new_password("x" * 11)
    with pytest.raises(ValueError):
        accounts.check_new_password("x" * 257)
    accounts.check_new_password("x" * 12)


def test_an_address_nobody_confirmed_cannot_sign_in(conn):
    accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    with pytest.raises(accounts.NotConfirmed):
        accounts.sign_in(conn, EMAIL, PASSWORD, NOON)


def test_a_wrong_password_on_an_unconfirmed_account_reveals_nothing(conn):
    accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.sign_in(conn, EMAIL, "not the password", NOON) is None


def test_a_confirmation_link_works_once_and_starts_a_session(conn):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    session, account_id = accounts.confirm_email(conn, token, NOON)

    assert accounts.session_account(conn, session, NOON) == (account_id, EMAIL)
    assert accounts.confirm_email(conn, token, NOON) is None


def test_a_confirmation_link_expires_after_24_hours(conn):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.link_email(conn, token, accounts.CONFIRM, NOON + timedelta(hours=23)) == EMAIL
    assert accounts.confirm_email(conn, token, NOON + timedelta(hours=24)) is None


def test_looking_at_a_link_does_not_use_it_up(conn):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.link_email(conn, token, accounts.CONFIRM, NOON) == EMAIL
    assert accounts.confirm_email(conn, token, NOON) is not None


def test_a_link_only_does_what_it_was_made_for(conn):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.reset_password(conn, token, NEW_PASSWORD, NOON) is None
    assert accounts.link_email(conn, token, accounts.RESET, NOON) is None


def test_an_unknown_link_does_nothing(conn):
    assert accounts.confirm_email(conn, "made-up", NOON) is None
    assert accounts.reset_password(conn, "made-up", NEW_PASSWORD, NOON) is None


def test_passwords_and_tokens_are_stored_only_as_hashes(conn, db):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)
    session, _ = accounts.confirm_email(conn, token, NOON)

    db.commit()
    stored = db.execute(
        "SELECT a.password_hash, t.token_hash, s.token_hash FROM accounts a"
        " JOIN email_tokens t ON t.account_id = a.id JOIN sessions s ON s.account_id = a.id"
    ).fetchone()
    assert PASSWORD not in stored[0] and stored[1] != token and stored[2] != session
    assert stored[1] == accounts.hashed(token) and stored[2] == accounts.hashed(session)


def test_confirmed_accounts_sign_in_with_the_right_password_only(conn):
    accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)

    assert accounts.sign_in(conn, EMAIL, PASSWORD, NOON) is not None
    assert accounts.sign_in(conn, EMAIL, "not the password", NOON) is None
    assert accounts.sign_in(conn, "nobody@example.com", PASSWORD, NOON) is None


def test_signing_up_again_before_confirming_replaces_the_password_and_the_link(conn):
    first = accounts.sign_up(conn, EMAIL, "someone elses password", NOON)
    second = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.confirm_email(conn, first, NOON) is None
    accounts.confirm_email(conn, second, NOON)
    assert accounts.sign_in(conn, EMAIL, "someone elses password", NOON) is None
    assert accounts.sign_in(conn, EMAIL, PASSWORD, NOON) is not None


def test_signing_up_with_a_confirmed_address_changes_nothing(conn):
    accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)

    assert accounts.sign_up(conn, EMAIL, "an attackers password", NOON) is None
    assert accounts.sign_in(conn, EMAIL, "an attackers password", NOON) is None
    assert accounts.sign_in(conn, EMAIL, PASSWORD, NOON) is not None


def test_unconfirmed_accounts_are_deleted_after_7_days(conn, db):
    accounts.sign_up(conn, EMAIL, PASSWORD, NOON)
    accounts.sign_up(conn, "recent@example.com", PASSWORD, NOON + timedelta(days=6))

    accounts.sign_up(conn, "later@example.com", PASSWORD, NOON + timedelta(days=7, seconds=1))

    db.commit()
    assert {email for (email,) in db.execute("SELECT email FROM accounts").fetchall()} == {
        "recent@example.com", "later@example.com"}


def test_confirmed_accounts_are_not_deleted_for_age(conn, db):
    accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)

    accounts.sign_up(conn, "later@example.com", PASSWORD, NOON + timedelta(days=30))

    db.commit()
    assert db.execute("SELECT count(*) FROM accounts WHERE email = %s", (EMAIL,)).fetchone() == (1,)


def test_a_new_password_replaces_the_old_and_ends_other_sessions(conn):
    old_session, _ = accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)
    token = accounts.ask_for_reset(conn, EMAIL, NOON)

    new_session, _ = accounts.reset_password(conn, token, NEW_PASSWORD, NOON)

    assert accounts.session_account(conn, old_session, NOON) is None
    assert accounts.session_account(conn, new_session, NOON) is not None
    assert accounts.sign_in(conn, EMAIL, PASSWORD, NOON) is None
    assert accounts.sign_in(conn, EMAIL, NEW_PASSWORD, NOON) is not None
    assert accounts.reset_password(conn, token, "yet another password", NOON) is None


def test_a_new_password_link_expires_after_an_hour(conn):
    accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)
    token = accounts.ask_for_reset(conn, EMAIL, NOON)

    assert accounts.reset_password(conn, token, NEW_PASSWORD, NOON + timedelta(hours=1)) is None


def test_only_confirmed_accounts_get_a_new_password_link(conn):
    accounts.sign_up(conn, EMAIL, PASSWORD, NOON)

    assert accounts.ask_for_reset(conn, EMAIL, NOON) is None
    assert accounts.ask_for_reset(conn, "nobody@example.com", NOON) is None


def test_sessions_last_30_days_and_end_on_sign_out(conn):
    session, _ = accounts.confirm_email(conn, accounts.sign_up(conn, EMAIL, PASSWORD, NOON), NOON)

    assert accounts.session_account(conn, session, NOON + timedelta(days=29)) is not None
    assert accounts.session_account(conn, session, NOON + timedelta(days=30)) is None
    accounts.sign_out(conn, session)
    assert accounts.session_account(conn, session, NOON) is None


def test_deleting_an_account_removes_everything_stored_for_it(conn, db):
    token = accounts.sign_up(conn, EMAIL, PASSWORD, NOON)
    _, account_id = accounts.confirm_email(conn, token, NOON)
    accounts.ask_for_reset(conn, EMAIL, NOON)
    counter = f"account:{account_id}"
    conn.execute("INSERT INTO question_counts VALUES (%s, 'day', %s, 1)", (counter, NOON))

    accounts.delete_account(conn, account_id, counter)

    db.commit()
    for table in ("accounts", "email_tokens", "sessions"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table
    assert db.execute("SELECT count(*) FROM question_counts WHERE visitor = %s", (counter,)).fetchone() == (0,)


# --- mailer.py -------------------------------------------------------------------

@responses.activate
def test_the_confirmation_email_carries_the_link_and_the_logo(monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    monkeypatch.setattr(config, "SIGN_IN_EMAIL_FROM", "Market Trends <sign-in@adnsaudio.com>")
    responses.add(responses.POST, mailer.RESEND_URL, json={"id": "x"})
    link = "https://api.example/api/sign-up/confirm?token=abc&x=1"

    mailer.send_confirmation(EMAIL, link)

    request = responses.calls[0].request
    assert request.headers["Authorization"] == "Bearer re_test"
    body = json.loads(request.body)
    assert body["from"] == "Market Trends <sign-in@adnsaudio.com>" and body["to"] == [EMAIL]
    assert link in body["text"]
    assert "token=abc&amp;x=1" in body["html"]  # escaped for HTML
    logo = body["attachments"][0]
    assert logo["content_id"] == mailer.LOGO_ID and f'src="cid:{mailer.LOGO_ID}"' in body["html"]
    assert 'alt="ADNS Audio"' in body["html"]
    assert base64.b64decode(logo["content"]) == mailer.LOGO_FILE.read_bytes()
    assert base64.b64decode(logo["content"]).startswith(b"\x89PNG")


def test_the_logo_is_small_and_comes_after_the_message():
    html = mailer.html_message("Title", ["Some text"], ("Press", "https://api.example/x"))

    assert html.index("Some text") < html.index("Press") < html.index(f"cid:{mailer.LOGO_ID}")
    assert 'width="80"' in html


@responses.activate
@pytest.mark.parametrize("send", [
    lambda: mailer.send_reset(EMAIL, "https://api.example/reset?token=abc"),
    lambda: mailer.send_already_registered(EMAIL),
])
def test_every_email_goes_through_resend_with_the_logo(monkeypatch, send):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    responses.add(responses.POST, mailer.RESEND_URL, json={"id": "x"})

    send()

    assert json.loads(responses.calls[0].request.body)["attachments"][0]["content_id"] == mailer.LOGO_ID


@responses.activate
@pytest.mark.parametrize("reply", [
    {"status": 403, "json": {"message": "secret detail"}},
    {"body": requests.ConnectionError("down")},
])
def test_a_refused_or_failed_send_is_reported(monkeypatch, reply):
    monkeypatch.setattr(config, "RESEND_API_KEY", "re_test")
    responses.add(responses.POST, mailer.RESEND_URL, **reply)

    with pytest.raises(mailer.MailUnavailable) as caught:
        mailer.send_confirmation(EMAIL, "https://api.example/x")
    assert "secret detail" not in str(caught.value)


def test_without_a_key_nothing_is_sent_unless_links_go_to_the_log(monkeypatch, caplog):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", False)
    with pytest.raises(mailer.MailUnavailable):
        mailer.send_confirmation(EMAIL, "https://api.example/x?token=abc")

    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", True)
    mailer.send_confirmation(EMAIL, "https://api.example/x?token=abc")
    assert "token=abc" in caplog.text


# --- Through the web API ----------------------------------------------------------

class Fakes:
    def __init__(self):
        self.mails = []
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

    def _keep(self, kind, email, link=None):
        if self.mail_error:
            raise self.mail_error
        self.mails.append((kind, email, link))

    def send_confirmation(self, email, link):
        self._keep("confirm", email, link)

    def send_reset(self, email, link):
        self._keep("reset", email, link)

    def send_already_registered(self, email):
        self._keep("already", email)


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
    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, mail=fakes, now=clock)
    return app.test_client()


def sign_up(client, email=EMAIL, password=PASSWORD, ip="203.0.113.7"):
    return client.post("/api/sign-up", json={"email": email, "password": password, "captcha_token": "good"},
                       environ_base={"REMOTE_ADDR": ip})


def sign_in_with_password(client, email=EMAIL, password=PASSWORD, ip="203.0.113.7"):
    return client.post("/api/sign-in", json={"email": email, "password": password, "captcha_token": "good"},
                       environ_base={"REMOTE_ADDR": ip})


def ask_for_reset(client, email=EMAIL, ip="203.0.113.7"):
    return client.post("/api/password-reset", json={"email": email, "captcha_token": "good"},
                       environ_base={"REMOTE_ADDR": ip})


def token_in(link):
    return parse_qs(urlsplit(link).query)["token"][0]


def nonce_in(page):
    return re.search(r'name="nonce" value="([^"]+)"', page.get_data(as_text=True)).group(1)


def press_confirm(client, link):
    token = token_in(link)
    page = client.get(f"/api/sign-up/confirm?token={token}")
    return client.post("/api/sign-up/confirm", data={"token": token, "nonce": nonce_in(page)})


def sign_in(client, fakes, email=EMAIL):
    """Sign up and confirm: the whole way to a session."""
    assert sign_up(client, email).status_code == 202
    return press_confirm(client, fakes.mails[-1][2])


def ask(client, ip="203.0.113.7", token="good"):
    return client.post("/api/questions", json={"question": "Cheapest?", "captcha_token": token},
                       environ_base={"REMOTE_ADDR": ip})


def test_signing_up_emails_a_confirmation_link_and_signs_nobody_in(client, fakes):
    response = sign_up(client, " Listener@Example.COM ")

    assert response.status_code == 202
    kind, email, link = fakes.mails[0]
    assert (kind, email) == ("confirm", EMAIL)
    assert link.startswith("https://api.example/api/sign-up/confirm?token=")
    assert client.get("/api/account").get_json()["signed_in"] is False


def test_signing_up_keeps_spaces_in_a_password(client, fakes):
    sign_up(client, password="  spaces at both ends  ")
    press_confirm(client, fakes.mails[-1][2])

    assert sign_in_with_password(client, password="  spaces at both ends  ").status_code == 200
    assert sign_in_with_password(client, password="spaces at both ends").status_code == 401


def test_the_answer_does_not_show_whether_an_address_has_an_account(client, fakes):
    new = sign_up(client, "new@example.com", ip="198.51.100.1")
    press_confirm(client, fakes.mails[-1][2])

    existing = sign_up(client, "new@example.com", ip="198.51.100.2")

    assert (new.status_code, new.get_json()) == (existing.status_code, existing.get_json())
    assert fakes.mails[-1] == ("already", "new@example.com", None)


@pytest.mark.parametrize("body, code", [
    ({"email": "nonsense", "password": PASSWORD, "captcha_token": "good"}, "invalid_email"),
    ({"email": EMAIL, "password": "too short", "captcha_token": "good"}, "invalid_password"),
    ({"email": EMAIL, "password": "", "captcha_token": "good"}, "invalid_password"),
    ({"email": EMAIL, "password": PASSWORD}, "invalid_request"),
    ({"email": EMAIL, "password": PASSWORD, "captcha_token": "good", "extra": 1}, "invalid_request"),
])
def test_bad_sign_ups_send_nothing(client, fakes, body, code):
    response = client.post("/api/sign-up", json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code
    assert fakes.mails == []


def test_a_failed_human_check_sends_nothing(client, fakes):
    fakes.human = False

    assert sign_up(client).status_code == 403
    assert ask_for_reset(client).status_code == 403
    assert fakes.mails == []


def test_emails_are_limited_per_address(client, fakes):
    codes = [sign_up(client, ip=f"198.51.100.{n}").status_code for n in range(4)]

    assert codes == [202, 202, 202, 429]
    assert len(fakes.mails) == 3


def test_emails_are_limited_per_visitor(client, fakes):
    codes = [sign_up(client, f"person{n}@example.com").status_code for n in range(6)]

    assert codes == [202] * 5 + [429]


def test_without_email_set_up_signing_up_says_so(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "RESEND_API_KEY", "")
    monkeypatch.setattr(config, "SIGN_IN_LOG_LINKS", False)

    response = sign_up(client)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "sign_in_unavailable"


def test_a_send_failure_is_reported(client, fakes):
    fakes.mail_error = mailer.MailUnavailable("Resend did not accept the email (status 500)")

    response = sign_up(client)

    assert response.status_code == 503
    assert "Resend" not in response.get_json()["error"]["message"]


def test_opening_the_confirmation_link_shows_a_page_and_changes_nothing(client, fakes):
    sign_up(client)
    token = token_in(fakes.mails[0][2])

    page = client.get(f"/api/sign-up/confirm?token={token}")

    assert page.status_code == 200 and EMAIL in page.get_data(as_text=True)
    assert client.get("/api/account").get_json()["signed_in"] is False
    assert sign_in_with_password(client).get_json()["error"]["code"] == "email_not_confirmed"


def test_the_confirm_page_escapes_the_address(client, fakes):
    sign_up(client, "<b>x@example.com")

    page = client.get(f"/api/sign-up/confirm?token={token_in(fakes.mails[0][2])}").get_data(as_text=True)

    assert "<b>x@example.com" not in page and "&lt;b&gt;x@example.com" in page


def test_the_confirm_page_cannot_be_framed_or_run_scripts(client, fakes):
    sign_up(client)

    policy = client.get(f"/api/sign-up/confirm?token={token_in(fakes.mails[0][2])}").headers["Content-Security-Policy"]

    assert "default-src 'none'" in policy and "frame-ancestors 'none'" in policy


def test_confirming_returns_to_the_site_verified_and_signed_in(client, fakes):
    response = sign_in(client, fakes)

    assert response.status_code == 303
    assert response.headers["Location"] == "https://site.example/market-trends/?verified=1"
    account = client.get("/api/account").get_json()
    assert account["signed_in"] is True and account["email"] == EMAIL and account["verified"] is True
    assert account["limits"]["day"] == config.QUESTIONS_PER_ACCOUNT_DAY


def test_later_visits_use_the_password(client, fakes):
    sign_in(client, fakes)
    client.post("/api/sign-out", json={})

    response = sign_in_with_password(client, " LISTENER@example.com")

    assert response.status_code == 200
    assert client.get("/api/account").get_json()["verified"] is True


def test_a_wrong_password_and_an_unknown_address_get_the_same_answer(client, fakes):
    sign_in(client, fakes)
    client.post("/api/sign-out", json={})

    wrong = sign_in_with_password(client, password="not the password")
    unknown = sign_in_with_password(client, "nobody@example.com")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json() == unknown.get_json()
    assert client.get("/api/account").get_json()["signed_in"] is False


def test_tries_at_a_password_are_limited_and_need_the_human_check(client, fakes):
    fakes.human = False
    assert sign_in_with_password(client).status_code == 403
    fakes.human = True

    codes = [sign_in_with_password(client, password="not the password", ip=f"198.51.100.{n}").status_code
             for n in range(11)]

    assert codes == [401] * 10 + [429]


def test_the_session_cookie_is_https_only_by_default(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)

    cookie = next(c for c in sign_in(client, fakes).headers.getlist("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))

    assert "Secure" in cookie and "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/api" in cookie


def test_a_confirm_posted_from_another_site_is_refused(client, fakes):
    sign_up(client)
    token = token_in(fakes.mails[0][2])

    response = client.post("/api/sign-up/confirm", data={"token": token, "nonce": "forged"})

    assert response.status_code == 403
    assert sign_in_with_password(client).get_json()["error"]["code"] == "email_not_confirmed"


def test_a_used_or_expired_link_shows_a_page_to_ask_again(client, fakes, clock):
    sign_in(client, fakes)
    token = token_in(fakes.mails[0][2])
    assert client.get(f"/api/sign-up/confirm?token={token}").status_code == 400

    sign_up(client, "other@example.com")
    clock.now = NOON + timedelta(hours=25)
    page = client.get(f"/api/sign-up/confirm?token={token_in(fakes.mails[-1][2])}")
    assert page.status_code == 400 and "has expired or was already used" in page.get_data(as_text=True)


def test_forgotten_password_from_the_email_to_signed_in_again(client, fakes):
    sign_in(client, fakes)
    client.post("/api/sign-out", json={})

    assert ask_for_reset(client).status_code == 202
    kind, _, link = fakes.mails[-1]
    assert kind == "reset"
    token = token_in(link)
    page = client.get(f"/api/password-reset/confirm?token={token}")
    assert page.status_code == 200
    assert client.get("/api/account").get_json()["signed_in"] is False

    response = client.post("/api/password-reset/confirm",
                           data={"token": token, "nonce": nonce_in(page), "password": NEW_PASSWORD})

    assert response.status_code == 303
    assert response.headers["Location"] == "https://site.example/market-trends/?password_changed=1"
    assert client.get("/api/account").get_json()["signed_in"] is True
    assert sign_in_with_password(client).status_code == 401
    assert sign_in_with_password(client, password=NEW_PASSWORD).status_code == 200


def test_asking_for_a_new_password_does_not_show_who_has_an_account(client, fakes):
    sign_in(client, fakes)
    count = len(fakes.mails)

    known = ask_for_reset(client, ip="198.51.100.1")
    unknown = ask_for_reset(client, "nobody@example.com", ip="198.51.100.2")

    assert (known.status_code, known.get_json()) == (unknown.status_code, unknown.get_json())
    assert len(fakes.mails) == count + 1


def test_a_short_new_password_shows_the_form_again_and_keeps_the_link(client, fakes):
    sign_in(client, fakes)
    ask_for_reset(client)
    token = token_in(fakes.mails[-1][2])
    page = client.get(f"/api/password-reset/confirm?token={token}")

    refused = client.post("/api/password-reset/confirm", data={"token": token, "nonce": nonce_in(page), "password": "short"})

    assert refused.status_code == 400 and "12 to 256" in refused.get_data(as_text=True)
    done = client.post("/api/password-reset/confirm", data={"token": token, "nonce": nonce_in(refused), "password": NEW_PASSWORD})
    assert done.status_code == 303


def test_a_new_password_posted_from_another_site_is_refused(client, fakes):
    sign_in(client, fakes)
    client.post("/api/sign-out", json={})
    ask_for_reset(client)

    response = client.post("/api/password-reset/confirm",
                           data={"token": token_in(fakes.mails[-1][2]), "nonce": "forged", "password": NEW_PASSWORD})

    assert response.status_code == 403
    assert sign_in_with_password(client).status_code == 200


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
        "signed_in": False, "email": None, "verified": False,
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
        " WHERE period = 'day' AND visitor <> 'site' AND visitor NOT LIKE 'sign-in-%' AND visitor NOT LIKE 'mail-%'"
    ).fetchall()
    assert questions == [("account:1", 2)]


def test_setting_up_the_login_replaces_older_accounts(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("DROP TABLE email_tokens")
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
    db.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'hash', now())")
    db.rollback()


def test_setting_up_the_login_again_keeps_current_accounts(db, limits_role):
    from ingestion.create_limits_role import create_limits_role
    from tests.conftest import FAKE_SETTINGS

    db.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'hash', now())")
    create_limits_role(db, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])
    db.commit()

    assert db.execute("SELECT email FROM accounts").fetchall() == [("a@example.com",)]
