"""The web API: request checks, the human check, limits, errors and headers.

The question service and Cloudflare are replaced by fakes; the limits run against
the real test database through the counting login.
"""
import logging
from datetime import datetime, timezone

import psycopg
import pytest
import requests
import responses

from queries.questions import MAX_QUESTION_LENGTH, Answer, QuestionError
from web import captcha, config, limits
from web.app import create_app

NOON = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)
ANSWER = Answer(
    text="Throw is the cheapest at €10.00.",
    sources=[{"tool": "rank_releases", "arguments": {"sort_by": "lowest_price"}, "result": {"rows": [], "total": 0}}],
    rounds=2, model="claude-haiku-5-5", usage={"input_tokens": 1234, "output_tokens": 56},
)


class Fakes:
    def __init__(self):
        self.questions = []
        self.tokens = []
        self.human = True
        self.answer_with = ANSWER

    def answer(self, question):
        self.questions.append(question)
        if isinstance(self.answer_with, Exception):
            raise self.answer_with
        return self.answer_with

    def verify(self, token, secret, remote_ip):
        self.tokens.append((token, secret, remote_ip))
        if isinstance(self.human, Exception):
            raise self.human
        return self.human


@pytest.fixture
def fakes():
    return Fakes()


@pytest.fixture
def client(fakes, limits_db):
    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, now=lambda: NOON)
    return app.test_client()


def ask(client, question="Cheapest Planet E record?", token="good-token", ip="203.0.113.7", **kwargs):
    body = {"question": question, "captcha_token": token}
    return client.post("/api/questions", json=body, environ_base={"REMOTE_ADDR": ip}, **kwargs)


def test_a_question_is_answered_with_what_the_page_needs(client, fakes):
    response = ask(client, question="  Cheapest Planet E record?  ")

    assert response.status_code == 200
    assert response.get_json() == {
        "answer": ANSWER.text, "sources": ANSWER.sources, "warning": None, "model": "claude-haiku-5-5",
        "remaining": {"hour": config.QUESTIONS_PER_VISITOR_HOUR - 1, "day": config.QUESTIONS_PER_VISITOR_DAY - 1},
    }
    assert fakes.questions == ["Cheapest Planet E record?"]
    assert fakes.tokens == [("good-token", config.TURNSTILE_SECRET_KEY, "203.0.113.7")]


def test_rounds_and_token_usage_never_reach_the_browser(client):
    body = ask(client).get_data(as_text=True)

    assert "usage" not in body and "rounds" not in body and "1234" not in body


def test_an_unverified_answer_carries_its_warning(client, fakes):
    fakes.answer_with = Answer(text="It costs €12.50.", warning="Some figures could not be checked.", model="m")

    assert ask(client).get_json()["warning"] == "Some figures could not be checked."


@pytest.mark.parametrize("send, code", [
    ({"data": "not json", "content_type": "text/plain"}, "invalid_request"),
    ({"json": ["a", "list"]}, "invalid_request"),
    ({"json": {"question": "Hi?", "captcha_token": "t", "model": "claude-opus-5-5"}}, "invalid_request"),
    ({"json": {"question": "   ", "captcha_token": "t"}}, "invalid_question"),
    ({"json": {"question": "x" * (MAX_QUESTION_LENGTH + 1), "captcha_token": "t"}}, "invalid_question"),
    ({"json": {"question": 42, "captcha_token": "t"}}, "invalid_question"),
])
def test_bad_requests_are_refused_before_any_check_or_question(client, fakes, send, code):
    response = client.post("/api/questions", **send)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == code
    assert fakes.tokens == [] and fakes.questions == []


def test_a_request_body_over_8_kb_is_refused(client, fakes):
    response = client.post("/api/questions", data="x" * 9000, content_type="application/json")

    assert response.status_code == 413
    assert fakes.questions == []


def test_a_failed_human_check_is_refused_and_not_counted(client, fakes, db):
    fakes.human = False

    response = ask(client)

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "captcha_failed"
    assert fakes.questions == []
    assert db.execute("SELECT count(*) FROM limit_counts").fetchone() == (0,)


def test_a_question_without_a_human_check_token_is_refused_when_not_signed_in(client, fakes, db):
    response = client.post("/api/questions", json={"question": "Hi?"})

    assert response.status_code == 403
    assert response.get_json()["error"]["code"] == "captcha_failed"
    assert fakes.tokens == [] and fakes.questions == []
    assert db.execute("SELECT count(*) FROM limit_counts").fetchone() == (0,)


def test_an_unreachable_cloudflare_refuses_the_question(client, fakes):
    fakes.human = captcha.CaptchaUnavailable("timeout")

    response = ask(client)

    assert response.status_code == 503 and response.headers["Retry-After"] == "30"
    assert response.get_json()["error"]["code"] == "captcha_unavailable"
    assert fakes.questions == []


def test_a_visitor_over_the_hourly_limit_is_refused_before_the_question_service(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 2)

    assert [ask(client).status_code for _ in range(3)] == [200, 200, 429]
    response = ask(client)

    assert response.get_json()["error"]["code"] == "visitor_limit_reached"
    assert "Sign up or sign in" in response.get_json()["error"]["message"]
    assert response.headers["Retry-After"] == str(30 * 60 + 1)
    assert len(fakes.questions) == 2
    assert ask(client, ip="198.51.100.1").status_code == 200  # another visitor


def test_the_site_wide_cap_stops_every_visitor(client, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_SITE_DAY", 3)

    statuses = [ask(client, ip=f"198.51.100.{n}").status_code for n in range(4)]

    assert statuses == [200, 200, 200, 429]
    assert ask(client, ip="192.0.2.99").get_json()["error"]["code"] == "site_limit_reached"


def test_visitors_cannot_dodge_the_limit_with_a_forged_forwarded_header(client, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)

    ask(client)
    response = ask(client, headers={"X-Forwarded-For": "10.9.8.7"})

    assert response.status_code == 429


def test_behind_a_trusted_proxy_the_forwarded_address_identifies_the_visitor(fakes, limits_db, monkeypatch):
    monkeypatch.setattr(config, "TRUSTED_PROXIES", 1)
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)
    client = create_app(answer=fakes.answer, verify_captcha=fakes.verify, now=lambda: NOON).test_client()

    assert ask(client, ip="10.0.0.1", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 200
    assert ask(client, ip="10.0.0.1", headers={"X-Forwarded-For": "203.0.113.8"}).status_code == 200
    assert ask(client, ip="10.0.0.1", headers={"X-Forwarded-For": "203.0.113.7"}).status_code == 429
    assert fakes.tokens[0][2] == "203.0.113.7"


def test_addresses_are_never_stored(client, db):
    ask(client, ip="203.0.113.7")

    visitors = [row[0] for row in db.execute("SELECT visitor FROM limit_counts").fetchall()]
    assert "203.0.113.7" not in visitors and "203.0.113.7" not in "".join(visitors)


def test_when_the_counts_cannot_be_reached_no_question_is_answered(fakes, limits_db):
    def broken():
        raise psycopg.OperationalError("connection refused")

    client = create_app(answer=fakes.answer, verify_captcha=fakes.verify, connect_limits=broken).test_client()

    response = ask(client)

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "database"
    assert fakes.questions == []


@pytest.mark.parametrize("code, status, retry_after", [
    ("invalid_question", 400, None),
    ("busy", 503, "60"),
    ("unavailable", 503, "60"),
    ("database", 503, "60"),
    ("configuration", 500, None),
])
def test_question_service_errors_become_http_errors(client, fakes, code, status, retry_after):
    fakes.answer_with = QuestionError(code, "A visitor-safe message.")

    response = ask(client)

    assert response.status_code == status
    assert response.get_json() == {"error": {"code": code, "message": "A visitor-safe message."}}
    assert response.headers.get("Retry-After") == retry_after


def test_an_unexpected_error_reveals_nothing(client, fakes):
    fakes.answer_with = RuntimeError("password=hunter2 at /srv/app/secret.py")

    response = ask(client)

    assert response.status_code == 500
    assert "hunter2" not in response.get_data(as_text=True)
    assert response.get_json()["error"]["code"] == "internal_error"


@pytest.mark.parametrize("method, path, status", [("get", "/api/questions", 405), ("get", "/nope", 404)])
def test_other_paths_and_methods_answer_in_json(client, method, path, status):
    response = getattr(client, method)(path)

    assert response.status_code == status
    assert "error" in response.get_json()


def test_health_check(client):
    assert client.get("/api/health").get_json() == {"status": "ok"}


def test_only_allowed_origins_may_call_from_the_browser(client, monkeypatch):
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://adnsaudio.com"])

    allowed = client.options("/api/questions", headers={"Origin": "https://adnsaudio.com"})
    other = client.options("/api/questions", headers={"Origin": "https://evil.example"})

    assert allowed.headers["Access-Control-Allow-Origin"] == "https://adnsaudio.com"
    assert "POST" in allowed.headers["Access-Control-Allow-Methods"]
    assert allowed.headers["Access-Control-Allow-Credentials"] == "true"
    assert "Access-Control-Allow-Origin" not in other.headers


def test_the_page_may_read_how_long_to_wait(client, monkeypatch):
    # Without this, a browser on another origin hides Retry-After from the page.
    monkeypatch.setattr(config, "ALLOWED_ORIGINS", ["https://adnsaudio.com"])
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)

    ask(client, headers={"Origin": "https://adnsaudio.com"})
    refused = ask(client, headers={"Origin": "https://adnsaudio.com"})

    assert refused.status_code == 429 and refused.headers["Retry-After"]
    assert refused.headers["Access-Control-Expose-Headers"] == "Retry-After"


def test_responses_are_never_cached_or_sniffed(client):
    response = ask(client)

    assert response.headers["Cache-Control"] == "no-store"
    assert response.headers["X-Content-Type-Options"] == "nosniff"


# --- Turnstile verification against a faked Cloudflare -----------------------------

@responses.activate
def test_turnstile_sends_the_secret_and_the_visitors_address():
    responses.post(captcha.VERIFY_URL, json={"success": True})

    assert captcha.verify("token-1", "our-secret", "203.0.113.7") is True
    sent = responses.calls[0].request.body
    assert "secret=our-secret" in sent and "response=token-1" in sent and "remoteip=203.0.113.7" in sent


@responses.activate
def test_turnstile_rejects_a_bad_token():
    responses.post(captcha.VERIFY_URL, json={"success": False, "error-codes": ["invalid-input-response"]})

    assert captcha.verify("forged", "our-secret") is False


@responses.activate
@pytest.mark.parametrize("reply", [
    {"body": requests.ConnectionError("down")},
    {"status": 500, "json": {}},
    {"body": "not json"},
])
def test_turnstile_trouble_is_reported_not_passed(reply):
    responses.post(captcha.VERIFY_URL, **reply)

    with pytest.raises(captcha.CaptchaUnavailable):
        captcha.verify("token", "our-secret")


def with_releases(db, *label_ids):
    for label_id in label_ids:
        db.execute("INSERT INTO labels (id, name) VALUES (%s, 'x')", (label_id,))
        db.execute("INSERT INTO releases (id, title, label_id, is_physical) VALUES (%s, 'x', %s, true)",
                   (label_id, label_id))


def test_the_labels_are_the_active_tracked_ones_in_alphabetical_order(client, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name, active) VALUES "
               "(1, 'Tresor', true), (2, 'axis', true), (3, 'Paused Records', false)")
    with_releases(db, 1, 2, 3)
    db.commit()

    response = client.get("/api/labels", headers={"Origin": "https://adnsaudio.test"})

    assert response.status_code == 200
    assert response.get_json() == {"labels": ["axis", "Tresor"]}
    assert response.headers["Cache-Control"] == "public, max-age=300"


def test_the_labels_leave_out_the_numbers_discogs_adds(client, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES "
               "(1, 'Afterlife (6)'), (2, 'Emerald (4)'), (3, 'Studio (54) Records')")
    with_releases(db, 1, 2, 3)
    db.commit()

    assert client.get("/api/labels").get_json() == {"labels": ["Afterlife", "Emerald", "Studio (54) Records"]}


def test_two_labels_with_the_same_name_keep_their_numbers(client, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES "
               "(1, 'Wax (4)'), (2, 'Wax (9)'), (3, 'Tresor'), (4, 'Tresor (2)')")
    with_releases(db, 1, 2, 3, 4)
    db.commit()

    assert client.get("/api/labels").get_json() == {"labels": ["Tresor", "Tresor (2)", "Wax (4)", "Wax (9)"]}


def test_a_number_is_dropped_when_the_other_same_name_label_is_not_listed(client, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES (1, 'Wax (4)'), (2, 'Wax (9)')")
    with_releases(db, 1)
    db.commit()

    assert client.get("/api/labels").get_json() == {"labels": ["Wax"]}


def test_a_label_is_listed_once_its_releases_are_in_the_database(client, db, reader):
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES (1, 'Tresor'), (2, 'Added Tonight')")
    with_releases(db, 1)
    db.commit()

    assert client.get("/api/labels").get_json() == {"labels": ["Tresor"]}


def test_other_answers_are_still_never_cached(client):
    assert client.get("/api/health").headers["Cache-Control"] == "no-store"


def test_the_labels_fail_with_a_plain_message_when_the_database_is_down(fakes, limits_db):
    def broken():
        raise psycopg.OperationalError("connection to 10.0.0.1 refused")

    response = create_app(answer=fakes.answer, verify_captcha=fakes.verify, list_labels=broken).test_client().get("/api/labels")

    assert response.status_code == 503
    assert response.get_json()["error"]["code"] == "database"
    assert "10.0.0.1" not in response.get_data(as_text=True)


def test_the_access_log_has_the_visitors_hash_and_never_the_address(client, caplog):
    client.get("/api/health?secret=in-the-query", environ_base={"REMOTE_ADDR": "203.0.113.7"},
               headers={"User-Agent": "test-browser"})

    line = next(record.getMessage() for record in caplog.records if record.name == "web.access")
    assert line == f"{limits.keyed_hash('203.0.113.7', config.VISITOR_HASH_SECRET)} GET /api/health 200 test-browser"
    assert "203.0.113.7" not in caplog.text


def test_a_visitor_cannot_fake_a_line_in_the_access_log(client, caplog):
    client.get("/api/nothing%0Afake-line", headers={"User-Agent": "x" * 500})

    line = next(record.getMessage() for record in caplog.records if record.name == "web.access")
    assert "\n" not in line and "\\n" in line
    assert " 404 " in line and len(line) < 500


def test_why_cloudflare_refused_a_token_reaches_the_log(client):
    assert logging.getLogger("web.captcha").isEnabledFor(logging.INFO)
    assert logging.getLogger("web").handlers


def test_a_question_that_could_not_be_answered_is_given_back(client, fakes, db):
    fakes.answer_with = QuestionError("busy", "Too many questions at the moment. Please try again in a minute.")
    assert ask(client).status_code == 503
    fakes.answer_with = ANSWER

    response = ask(client)

    assert response.get_json()["remaining"] == {"hour": config.QUESTIONS_PER_VISITOR_HOUR - 1,
                                                "day": config.QUESTIONS_PER_VISITOR_DAY - 1}
    db.commit()
    assert db.execute("SELECT count FROM limit_counts WHERE visitor = 'site'").fetchone() == (1,)
