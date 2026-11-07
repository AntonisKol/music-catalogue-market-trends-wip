"""Sign-in with a username and password: accounts, sessions, limits and the command.

Everything runs against the real test database through the web login.
"""
from datetime import datetime, timedelta, timezone

import psycopg
import pytest

from ingestion.create_limits_role import create_limits_role
from tests.conftest import FAKE_SETTINGS
from web import accounts, config, limits, manage_accounts
from web.app import SESSION_COOKIE, create_app
from web.limits import connect_limits

NOON = datetime(2026, 11, 1, 12, 30, tzinfo=timezone.utc)
USERNAME = "listener"
PASSWORD = "correct horse battery"


# --- accounts.py on its own ------------------------------------------------------

@pytest.fixture
def conn(limits_db):
    with connect_limits() as conn:
        yield conn


@pytest.mark.parametrize("text, expected", [("  Listener ", "listener"), ("a.b-c_1", "a.b-c_1")])
def test_usernames_are_stored_lower_cased(text, expected):
    assert accounts.normalize_username(text) == expected


@pytest.mark.parametrize("text", ["", "ab", "two words", "<script>", "-dash", "x" * 65, "a@example.com"])
def test_what_cannot_be_a_username_is_refused(text):
    with pytest.raises(ValueError):
        accounts.normalize_username(text)


@pytest.mark.parametrize("password", ["", "short", "x" * 11, "x" * 257])
def test_a_password_of_the_wrong_length_is_refused(conn, password):
    with pytest.raises(ValueError):
        accounts.create_account(conn, USERNAME, password, NOON)


def test_the_right_password_starts_a_session(conn):
    account_id = accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    session, signed_in_id, username = accounts.sign_in(conn, " LISTENER ", PASSWORD, NOON)

    assert (signed_in_id, username) == (account_id, USERNAME)
    assert accounts.session_account(conn, session, NOON) == (account_id, USERNAME)


@pytest.mark.parametrize("username, password", [
    (USERNAME, "wrong password here"), ("nobody", PASSWORD), (USERNAME, PASSWORD + " "), (USERNAME, PASSWORD.upper()),
])
def test_a_wrong_sign_in_starts_nothing(conn, db, username, password):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    assert accounts.sign_in(conn, username, password, NOON) is None
    db.commit()
    assert db.execute("SELECT count(*) FROM sessions").fetchone() == (0,)


def test_an_unknown_username_still_checks_a_password_hash(conn, monkeypatch):
    checked = []
    monkeypatch.setattr(accounts, "check_password_hash", lambda stored, given: checked.append(stored) or False)

    assert accounts.sign_in(conn, "nobody", PASSWORD, NOON) is None
    assert checked == [accounts.UNUSABLE_HASH]


def test_a_username_can_be_taken_only_once(conn):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    with pytest.raises(accounts.AccountExists):
        accounts.create_account(conn, "LISTENER", "another password", NOON)


def test_passwords_and_sessions_are_stored_only_as_hashes(conn, db):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    session, _, _ = accounts.sign_in(conn, USERNAME, PASSWORD, NOON)

    db.commit()
    stored = str(db.execute("SELECT * FROM accounts").fetchall() + db.execute("SELECT * FROM sessions").fetchall())
    assert PASSWORD not in stored and session not in stored
    assert "scrypt:" in stored and accounts.hashed(session) in stored


def test_two_accounts_with_one_password_store_different_hashes(conn, db):
    accounts.create_account(conn, "first", PASSWORD, NOON)
    accounts.create_account(conn, "second", PASSWORD, NOON)

    db.commit()
    first, second = db.execute("SELECT password_hash FROM accounts").fetchall()
    assert first != second


def test_sessions_last_30_days_and_end_on_sign_out(conn):
    account_id = accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    session, _, _ = accounts.sign_in(conn, USERNAME, PASSWORD, NOON)

    assert accounts.session_account(conn, session, NOON + timedelta(days=29)) == (account_id, USERNAME)
    assert accounts.session_account(conn, session, NOON + timedelta(days=30)) is None
    accounts.sign_out(conn, session)
    assert accounts.session_account(conn, session, NOON) is None


def test_a_new_password_replaces_the_old_and_ends_sessions(conn):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    session, _, _ = accounts.sign_in(conn, USERNAME, PASSWORD, NOON)

    assert accounts.set_password(conn, USERNAME, "a brand new password") is True

    assert accounts.session_account(conn, session, NOON) is None
    assert accounts.sign_in(conn, USERNAME, PASSWORD, NOON) is None
    assert accounts.sign_in(conn, USERNAME, "a brand new password", NOON) is not None


def test_a_new_password_needs_an_account(conn):
    assert accounts.set_password(conn, "nobody", "a brand new password") is False


def test_deleting_an_account_removes_everything_stored_for_it(conn, db):
    account_id = accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    accounts.sign_in(conn, USERNAME, PASSWORD, NOON)
    conn.execute("INSERT INTO question_counts VALUES (%s, 'day', %s, 4)", (f"account:{account_id}", NOON))

    accounts.delete_account(conn, account_id, f"account:{account_id}")

    db.commit()
    for table in ("accounts", "sessions", "question_counts"):
        assert db.execute(f"SELECT count(*) FROM {table}").fetchone() == (0,), table


# --- Moving a database off email sign-in -----------------------------------------

def test_the_old_email_tables_and_addresses_are_removed(limits_db, db, postgres_url):
    db.execute("""
        DROP TABLE sessions; DROP TABLE accounts;
        CREATE TABLE accounts (id BIGSERIAL PRIMARY KEY, email TEXT NOT NULL UNIQUE,
                               created_at TIMESTAMPTZ NOT NULL, last_sign_in_at TIMESTAMPTZ NOT NULL);
        CREATE TABLE sign_in_tokens (token_hash TEXT PRIMARY KEY, email TEXT NOT NULL, created_at TIMESTAMPTZ NOT NULL,
                                     expires_at TIMESTAMPTZ NOT NULL, used_at TIMESTAMPTZ);
        CREATE TABLE sessions (token_hash TEXT PRIMARY KEY, account_id BIGINT NOT NULL REFERENCES accounts(id) ON DELETE CASCADE,
                               created_at TIMESTAMPTZ NOT NULL, expires_at TIMESTAMPTZ NOT NULL);
    """)
    db.execute("INSERT INTO accounts (email, created_at, last_sign_in_at) VALUES ('old@example.com', now(), now())")
    db.execute("INSERT INTO sign_in_tokens VALUES ('hash', 'old@example.com', now(), now())")
    db.execute("INSERT INTO sessions VALUES ('hash', 1, now(), now())")
    db.execute("INSERT INTO question_counts VALUES ('account:1', 'day', now(), 3), ('sign-in-email:x', 'day', now(), 1),"
               " ('some-visitor', 'day', now(), 2)")
    db.commit()

    for _ in range(2):  # running it again changes nothing
        with psycopg.connect(postgres_url) as admin:
            create_limits_role(admin, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])

    assert db.execute("SELECT to_regclass('sign_in_tokens')").fetchone() == (None,)
    columns = [row[0] for row in db.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name = 'accounts'").fetchall()]
    assert "email" not in columns and "password_hash" in columns
    assert db.execute("SELECT count(*) FROM accounts").fetchone() == (0,)
    assert db.execute("SELECT visitor FROM question_counts").fetchall() == [("some-visitor",)]
    db.commit()
    with connect_limits() as web:  # and the web login can use the new tables
        accounts.create_account(web, USERNAME, PASSWORD, NOON)
        assert accounts.sign_in(web, USERNAME, PASSWORD, NOON) is not None


def test_running_the_role_again_keeps_password_accounts(conn, postgres_url):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    with psycopg.connect(postgres_url) as admin:
        create_limits_role(admin, FAKE_SETTINGS["POSTGRES_LIMITS_USER"], FAKE_SETTINGS["POSTGRES_LIMITS_PASSWORD"])

    assert accounts.sign_in(conn, USERNAME, PASSWORD, NOON) is not None


# --- The command that manages accounts --------------------------------------------

@pytest.fixture
def typed(monkeypatch):
    """What is typed at the hidden password prompts."""
    answers = []
    monkeypatch.setattr(manage_accounts.getpass, "getpass", lambda prompt: answers.pop(0))
    return answers


def test_the_command_adds_an_account_that_can_sign_in(conn, typed, capsys):
    typed += [PASSWORD, PASSWORD]

    assert manage_accounts.main(["add", "Listener"]) == 0

    out = capsys.readouterr().out
    assert USERNAME in out and PASSWORD not in out
    assert accounts.sign_in(conn, USERNAME, PASSWORD, NOON) is not None


@pytest.mark.parametrize("first, second, expected", [
    (PASSWORD, "something else entirely", "not the same"), ("short", "short", "12 to 256"),
])
def test_the_command_refuses_a_bad_password(conn, typed, capsys, first, second, expected):
    typed += [first, second]

    assert manage_accounts.main(["add", USERNAME]) == 1

    assert expected in capsys.readouterr().err
    assert accounts.find_account(conn, USERNAME) is None


def test_the_command_will_not_add_a_taken_username(conn, typed, capsys):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    typed += ["another password", "another password"]

    assert manage_accounts.main(["add", USERNAME]) == 1
    assert "already an account" in capsys.readouterr().err
    assert accounts.sign_in(conn, USERNAME, PASSWORD, NOON) is not None


def test_the_command_resets_a_password(conn, typed):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)
    typed += ["a brand new password", "a brand new password"]

    assert manage_accounts.main(["set-password", USERNAME]) == 0

    assert accounts.sign_in(conn, USERNAME, "a brand new password", NOON) is not None


def test_the_command_lists_accounts_without_passwords(conn, capsys):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    assert manage_accounts.main(["list"]) == 0

    out = capsys.readouterr().out
    assert USERNAME in out and "never" in out and "scrypt" not in out


def test_the_command_deletes_an_account(conn):
    accounts.create_account(conn, USERNAME, PASSWORD, NOON)

    assert manage_accounts.main(["delete", USERNAME]) == 0

    assert accounts.find_account(conn, USERNAME) is None


@pytest.mark.parametrize("command", ["set-password", "delete"])
def test_the_command_says_when_there_is_no_such_account(conn, capsys, command):
    assert manage_accounts.main([command, "nobody"]) == 1
    assert "no account called nobody" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [[], ["add"], ["list", "x"], ["add", "a", "b"], ["promote", USERNAME]])
def test_the_command_explains_itself(argv, capsys):
    assert manage_accounts.main(argv) == 2
    assert "Usage" in capsys.readouterr().err


# --- Through the web API ----------------------------------------------------------

class Fakes:
    def __init__(self):
        self.questions = []
        self.tokens = []
        self.human = True

    def answer(self, question):
        from queries.questions import Answer
        self.questions.append(question)
        return Answer(text="An answer.", model="claude-haiku-5-5")

    def verify(self, token, secret, remote_ip):
        self.tokens.append(token)
        return self.human


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
def account(conn):
    return accounts.create_account(conn, USERNAME, PASSWORD, NOON)


@pytest.fixture
def client(fakes, clock, account, monkeypatch):
    # The test client talks plain http, which a Secure cookie would not be sent over.
    monkeypatch.setattr(config, "COOKIE_SECURE", False)
    app = create_app(answer=fakes.answer, verify_captcha=fakes.verify, now=clock)
    return app.test_client()


def sign_in(client, username=USERNAME, password=PASSWORD, ip="203.0.113.7"):
    return client.post("/api/sign-in", json={"username": username, "password": password, "captcha_token": "good"},
                       environ_base={"REMOTE_ADDR": ip})


def ask(client, ip="203.0.113.7", token="good"):
    return client.post("/api/questions", json={"question": "Cheapest?", "captcha_token": token},
                       environ_base={"REMOTE_ADDR": ip})


def session_cookie(response):
    return next(c for c in response.headers.getlist("Set-Cookie") if c.startswith(SESSION_COOKIE + "="))


def test_signing_in_starts_a_session_in_a_protected_cookie(client, fakes):
    response = sign_in(client)

    assert response.status_code == 200
    assert response.get_json() == {"status": "signed_in", "username": USERNAME}
    cookie = session_cookie(response)
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie and "Path=/api" in cookie
    assert fakes.tokens == ["good"]
    assert client.get("/api/account").get_json() == {
        "signed_in": True, "username": USERNAME,
        "limits": {"hour": config.QUESTIONS_PER_ACCOUNT_HOUR, "day": config.QUESTIONS_PER_ACCOUNT_DAY},
        "remaining": {"hour": config.QUESTIONS_PER_ACCOUNT_HOUR, "day": config.QUESTIONS_PER_ACCOUNT_DAY},
    }


def test_the_session_cookie_is_https_only_by_default(client, monkeypatch):
    monkeypatch.setattr(config, "COOKIE_SECURE", True)

    assert "Secure" in session_cookie(sign_in(client))


def test_an_unknown_username_and_a_wrong_password_get_the_same_answer(client):
    wrong = sign_in(client, password="wrong password here", ip="198.51.100.1")
    unknown = sign_in(client, username="nobody", ip="198.51.100.2")

    assert wrong.status_code == unknown.status_code == 401
    assert wrong.get_json() == unknown.get_json()
    assert wrong.get_json()["error"]["code"] == "invalid_credentials"
    assert "Set-Cookie" not in wrong.headers
    assert client.get("/api/account").get_json()["signed_in"] is False


def test_there_is_no_way_to_register(client, db):
    response = sign_in(client, username="newcomer", password="a password for me")

    assert response.status_code == 401
    db.commit()
    assert db.execute("SELECT username FROM accounts").fetchall() == [(USERNAME,)]
    assert client.post("/api/register", json={"username": "newcomer", "password": PASSWORD}).status_code == 404


@pytest.mark.parametrize("body", [
    {"username": USERNAME, "password": PASSWORD},
    {"username": USERNAME, "captcha_token": "good"},
    {"password": PASSWORD, "captcha_token": "good"},
    {"username": USERNAME, "password": PASSWORD, "captcha_token": "good", "admin": True},
    {"username": USERNAME, "password": "x" * 257, "captcha_token": "good"},
    {"username": "x" * 65, "password": PASSWORD, "captcha_token": "good"},
    {"username": USERNAME, "password": 12345, "captcha_token": "good"},
])
def test_bad_sign_in_requests_check_nothing(client, fakes, body):
    response = client.post("/api/sign-in", json=body)

    assert response.status_code == 400
    assert response.get_json()["error"]["code"] == "invalid_request"
    assert fakes.tokens == []


def test_an_error_never_repeats_the_password(client):
    response = client.post("/api/sign-in", json={"username": USERNAME, "password": "x" * 257 + "secret",
                                                 "captcha_token": "good"})

    assert "secret" not in response.get_data(as_text=True)


def test_signing_in_needs_a_json_request(client):
    response = client.post("/api/sign-in", data={"username": USERNAME, "password": PASSWORD, "captcha_token": "good"})

    assert response.status_code == 400
    assert client.get("/api/account").get_json()["signed_in"] is False


def test_a_failed_human_check_signs_nobody_in_and_counts_nothing(client, fakes, db):
    fakes.human = False

    assert sign_in(client).status_code == 403
    assert client.get("/api/account").get_json()["signed_in"] is False
    db.commit()
    assert db.execute("SELECT count(*) FROM question_counts WHERE visitor LIKE 'sign-in-%'").fetchone() == (0,)


def test_guessing_is_limited_per_username_from_any_address(client, monkeypatch):
    monkeypatch.setattr(limits, "SIGN_IN_PER_USERNAME", {"hour": 3, "day": 30})

    statuses = [sign_in(client, password="wrong password here", ip=f"198.51.100.{n}").status_code for n in range(4)]

    assert statuses == [401, 401, 401, 429]
    refused = sign_in(client, ip="198.51.100.200")  # even the right password, until the limit ends
    assert refused.status_code == 429
    assert refused.get_json()["error"]["code"] == "sign_in_limit_reached"
    assert refused.headers["Retry-After"] == str(30 * 60 + 1)


def test_guessing_is_limited_per_visitor_across_usernames(client, monkeypatch):
    monkeypatch.setattr(limits, "SIGN_IN_PER_VISITOR", {"hour": 3, "day": 30})

    statuses = [sign_in(client, username=f"person{n}").status_code for n in range(4)]

    assert statuses == [401, 401, 401, 429]
    assert sign_in(client, ip="198.51.100.9").status_code == 200  # another visitor


def test_a_session_ends_after_30_days(client, clock):
    sign_in(client)
    clock.now = NOON + timedelta(days=31)

    assert client.get("/api/account").get_json()["signed_in"] is False


def test_signed_in_questions_skip_the_human_check_and_count_per_account(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_ACCOUNT_HOUR", 2)
    sign_in(client)
    fakes.tokens.clear()

    responses_ = [ask(client, token="") for _ in range(3)]

    assert [r.status_code for r in responses_] == [200, 200, 429]
    assert responses_[0].get_json()["remaining"] == {"hour": 1, "day": config.QUESTIONS_PER_ACCOUNT_DAY - 1}
    assert responses_[2].get_json()["error"]["code"] == "account_limit_reached"
    assert fakes.tokens == []


def test_visitors_without_an_account_keep_their_small_allowance(client, fakes, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)

    assert ask(client).status_code == 200
    refused = ask(client)

    assert refused.get_json()["error"]["code"] == "visitor_limit_reached"
    assert "Sign in to ask more" in refused.get_json()["error"]["message"]
    assert fakes.questions == ["Cheapest?"]


def test_an_account_is_not_limited_by_its_address(client, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_DAY", 1)
    monkeypatch.setattr(config, "QUESTIONS_PER_VISITOR_HOUR", 1)
    assert ask(client).status_code == 200
    assert ask(client).status_code == 429

    sign_in(client)

    assert ask(client).status_code == 200


def test_the_site_cap_applies_to_accounts_too(client, monkeypatch):
    monkeypatch.setattr(config, "QUESTIONS_PER_SITE_DAY", 1)
    assert ask(client, ip="198.51.100.9").status_code == 200

    sign_in(client)

    assert ask(client).get_json()["error"]["code"] == "site_limit_reached"


def test_anonymous_visitors_see_what_is_left(client):
    ask(client, ip="203.0.113.7")

    assert client.get("/api/account", environ_base={"REMOTE_ADDR": "203.0.113.7"}).get_json() == {
        "signed_in": False, "username": None,
        "limits": {"hour": config.QUESTIONS_PER_VISITOR_HOUR, "day": config.QUESTIONS_PER_VISITOR_DAY},
        "remaining": {"hour": config.QUESTIONS_PER_VISITOR_HOUR - 1, "day": config.QUESTIONS_PER_VISITOR_DAY - 1},
    }


def test_signing_out_ends_the_session(client):
    sign_in(client)
    session = client.get_cookie(SESSION_COOKIE, path="/api").value

    response = client.post("/api/sign-out", json={})

    assert response.status_code == 200
    assert client.get("/api/account").get_json()["signed_in"] is False
    client.set_cookie(SESSION_COOKIE, session, path="/api")  # a copy of the old cookie
    assert client.get("/api/account").get_json()["signed_in"] is False


@pytest.mark.parametrize("path", ["/api/sign-out", "/api/account/delete"])
def test_sign_out_and_delete_need_a_json_request(client, path):
    sign_in(client)

    response = client.post(path, data="x", content_type="text/plain")

    assert response.status_code == 400
    assert client.get("/api/account").get_json()["signed_in"] is True


def test_deleting_the_account_signs_out_and_removes_it(client, db):
    sign_in(client)
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


def test_the_email_sign_in_routes_are_gone(client):
    assert client.post("/api/sign-in", json={"email": "a@example.com", "captcha_token": "good"}).status_code == 400
    assert client.get("/api/sign-in/confirm?token=x").status_code in (404, 405)
    assert client.post("/api/sign-in/confirm", data={"token": "x"}).status_code == 404


def test_signed_in_questions_count_against_the_account_not_the_address(client, db):
    sign_in(client)
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
