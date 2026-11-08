"""The web API the question page calls.

    flask --app web.app run                          # development
    gunicorn --workers 2 --timeout 120 'web.app:create_app()'   # production

POST /api/questions with {"question": "...", "captcha_token": "..."} returns
{"answer", "sources", "warning", "model", "remaining"}. Each question passes, in
this order: a check of the request, the Turnstile human check (skipped when
signed in), the visitor's or account's limits and the site's, and only then the
question service, so nothing reaches the Anthropic API before every guard has
passed. Errors come back as {"error": {"code", "message"}} with a fitting HTTP
status and never contain secrets or internal details; those go to the log.

GET /api/labels returns {"labels": [...]}, the labels the site follows whose releases
are in the database, for the list on the page. Signed-in visitors can ask for more (see web/label_requests.py):
    POST /api/label-requests     {"name"}: stores a request for the owner to review
    GET  /api/label-requests     this account's requests and their status

Each request is written to the access log with a keyed hash of the visitor's
address, never the address, so abuse can be spotted without keeping addresses.

Signing up (see web/accounts.py). Accounts can't be used until the address is confirmed:
    POST /api/sign-up            {"email", "password", "captcha_token"}: emails a confirmation link
    GET  /api/sign-up/confirm    the link: a page with a confirm button
    POST /api/sign-up/confirm    the button: confirms, starts a session, back to the site
    POST /api/sign-in            {"email", "password", "captcha_token"}: starts a session
    POST /api/password-reset     {"email", "captcha_token"}: emails a new-password link
    GET  /api/password-reset/confirm    the link: a page asking for the new password
    POST /api/password-reset/confirm    the form: changes it, starts a session, back to the site
    GET  /api/account            signed in or not, and the questions left
    POST /api/sign-out           ends this session
    POST /api/account/delete     deletes the account and everything stored for it
"""
import hmac
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import psycopg
from flask import Flask, jsonify, make_response, redirect, request
from markupsafe import escape
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from queries.labels import listed_label_names, tracked_label_names
from queries.questions import MAX_QUESTION_LENGTH, QuestionError, answer_question
from web import accounts, captcha, config, label_requests, limits, mailer

log = logging.getLogger(__name__)
access_log = logging.getLogger("web.access")

# How long browsers may reuse the list of labels, which changes rarely.
LABELS_CACHE_SECONDS = 300
LOGGED_TEXT_LENGTH = 200

MAX_REQUEST_BYTES = 8 * 1024

# Question service errors: HTTP status and, for temporary ones, seconds to wait.
QUESTION_ERRORS = {
    "invalid_question": (400, None),
    "busy": (503, 60),
    "unavailable": (503, 60),
    "database": (503, 60),
    "configuration": (500, None),
}


# A field that fails validation gets its own message; any other gets a general one.
FIELD_ERRORS = {
    "question": ("invalid_question", f"Please type a question of up to {MAX_QUESTION_LENGTH} characters."),
    "email": ("invalid_email", "Please type a valid email address."),
    "password": ("invalid_password", "Please type a password."),
}

SESSION_COOKIE = "mt_session"
CONFIRM_COOKIE = "mt_confirm"
CAPTCHA_TOKEN_LENGTH = 2048
# Room for spaces typed around an address; accounts.normalize_email trims them and
# then applies the real limit.
EMAIL_FIELD_LENGTH = accounts.MAX_EMAIL_LENGTH + 20


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not needed when signed in.
    captcha_token: str = Field(default="", max_length=CAPTCHA_TOKEN_LENGTH)


class LabelRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=label_requests.MAX_NAME_LENGTH * 2)


class AddressAndPassword(BaseModel):
    """Signing up and signing in. Passwords are taken exactly as typed, so spaces are not stripped."""
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=EMAIL_FIELD_LENGTH)
    password: str = Field(min_length=1, max_length=accounts.MAX_PASSWORD_LENGTH)
    captcha_token: str = Field(min_length=1, max_length=CAPTCHA_TOKEN_LENGTH)


class ResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    email: str = Field(min_length=3, max_length=EMAIL_FIELD_LENGTH)
    captcha_token: str = Field(min_length=1, max_length=CAPTCHA_TOKEN_LENGTH)


PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark"><title>{title} · Market Trends</title>
<style>
body {{ margin: 0; min-height: 100vh; display: grid; place-items: center; background: #07080b; color: #c9cdd6;
       font: 400 16px/1.6 "Chakra Petch", system-ui, sans-serif; }}
main {{ max-width: 440px; margin: 24px; padding: 28px; border: 1px solid #403553; background: #111318; }}
h1 {{ margin: 0 0 12px; color: #eceef2; font-size: 22px; }}
strong {{ color: #eceef2; word-break: break-all; }}
button {{ min-height: 44px; margin-top: 12px; padding: 10px 20px; border: 1px solid #b19bdd; background: #392850;
          color: #eceef2; font: inherit; font-size: 15px; font-weight: 600; cursor: pointer; }}
a {{ color: #b19bdd; }}
label {{ display: block; margin-top: 12px; color: #eceef2; }}
input[type=password] {{ box-sizing: border-box; width: 100%; min-height: 44px; margin-top: 6px; padding: 10px 12px;
                        border: 1px solid #30343b; background: #07080b; color: #eceef2; font: inherit; }}
.problem {{ color: #ff8a8a; }}
</style></head>
<body><main><h1>{title}</h1>{body}</main></body></html>
"""


def page(title: str, body: str, status: int = 200):
    """A small HTML page. Callers escape anything that came from outside."""
    response = make_response(PAGE.format(title=escape(title), body=body), status)
    response.headers["Content-Type"] = "text/html; charset=utf-8"
    return response


def link_used_page():
    return page("This link can't be used", (
        "<p>This link has expired or was already used. Confirmation links work for 24 hours and "
        "new-password links for an hour, once each.</p>"
        f'<p><a href="{escape(config.SIGN_IN_RETURN_URL)}">Back to the question box</a> to ask for a new one.</p>'
    ), 400)


def unavailable_page():
    return page("This is unavailable right now", "<p>Please open the link again in a minute.</p>", 503)


def back_to_the_site(flag: str, session: str, cookie_path: str):
    """Return to the question box, signed in. The address is fixed in the settings, never
    taken from the request, so links cannot redirect anywhere else."""
    url = config.SIGN_IN_RETURN_URL
    response = with_session(redirect(f"{url}{'&' if '?' in url else '?'}{flag}=1", 303), session)
    response.delete_cookie(CONFIRM_COOKIE, path=cookie_path, secure=config.COOKIE_SECURE,
                           httponly=True, samesite="Strict")
    return response


def with_session(response, session: str):
    """The session cookie: sent only to the API, only over HTTPS, never readable by scripts."""
    response.set_cookie(SESSION_COOKIE, session, max_age=int(accounts.SESSION_LIFETIME.total_seconds()),
                        path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
    return response


def with_nonce(html_response, nonce: str, cookie_path: str, lifetime):
    """A random value both in a cookie and in the form: a form posted from another site
    has no way to carry the cookie, so it is refused."""
    html_response.set_cookie(CONFIRM_COOKIE, nonce, max_age=int(lifetime.total_seconds()), path=cookie_path,
                             secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
    return html_response


def nonce_matches() -> bool:
    nonce = request.form.get("nonce", "")
    return bool(nonce) and hmac.compare_digest(nonce, request.cookies.get(CONFIRM_COOKIE, ""))


def error(code: str, message: str, status: int, retry_after: int = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def for_the_log(text: str) -> str:
    """Visitors choose the path and the browser name, so they can't be allowed to fake a log line."""
    return text[:LOGGED_TEXT_LENGTH].encode("unicode_escape").decode("ascii")


def set_up_logging() -> None:
    """The access log and the API's other messages, such as why Cloudflare refused a token."""
    web_log = logging.getLogger("web")
    if web_log.handlers:
        return
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter("%(asctime)s %(message)s"))
    web_log.addHandler(handler)
    web_log.setLevel(logging.INFO)


def create_app(answer=answer_question, verify_captcha=captcha.verify, connect_limits=limits.connect_limits,
               mail=mailer, now=lambda: datetime.now(timezone.utc),
               list_labels=listed_label_names, known_labels=tracked_label_names) -> Flask:
    """The app. The arguments exist so tests can replace the outside services."""
    set_up_logging()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if config.TRUSTED_PROXIES:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUSTED_PROXIES)

    def visitor_ip() -> str:
        return request.remote_addr or "unknown"

    def hashed(text: str) -> str:
        return limits.keyed_hash(text, config.VISITOR_HASH_SECRET)

    def session_token() -> str:
        return request.cookies.get(SESSION_COOKIE, "")

    def counter(account) -> str:
        return f"account:{account[0]}" if account else hashed(visitor_ip())

    def allowance(account) -> tuple:
        if account:
            return config.QUESTIONS_PER_ACCOUNT_HOUR, config.QUESTIONS_PER_ACCOUNT_DAY
        return config.QUESTIONS_PER_VISITOR_HOUR, config.QUESTIONS_PER_VISITOR_DAY

    def human_check(token: str):
        """None if the visitor passed the Turnstile check, otherwise the error response."""
        if not token:
            return error("captcha_failed", "The human check is missing. Please try again.", 403)
        try:
            human = verify_captcha(token, config.TURNSTILE_SECRET_KEY, visitor_ip())
        except captcha.CaptchaUnavailable as problem:
            log.error("Turnstile verification unavailable: %s", problem)
            return error("captcha_unavailable", "The human check is unavailable right now. Please try again.", 503, 30)
        if not human:
            return error("captcha_failed", "The human check failed. Please try again.", 403)
        return None

    def read_json(model, wanted: str):
        """(payload, None) from the request body, or (None, the error response to send)."""
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return None, error("invalid_request", f"Send a JSON object with {wanted}.", 400)
        try:
            return model.model_validate(body), None
        except ValidationError as problem:
            fields = sorted({str(e["loc"][0]) for e in problem.errors() if e["loc"]})
            for field, (code, message) in FIELD_ERRORS.items():
                if field in fields:
                    return None, error(code, message, 400)
            return None, error("invalid_request", f"Invalid request: {', '.join(fields) or 'body'}.", 400)

    def without_session(response):
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
        return response

    @app.post("/api/questions")
    def ask():
        payload, refused = read_json(QuestionRequest, "a question")
        if refused:
            return refused
        asked_at = now()
        with connect_limits() as conn:
            account = accounts.session_account(conn, session_token(), asked_at)
            if not account:
                refused = human_check(payload.captcha_token)
                if refused:
                    return refused
            per_hour, per_day = allowance(account)
            left = limits.take_question(
                conn, counter(account), asked_at, per_hour, per_day, config.QUESTIONS_PER_SITE_DAY,
                signed_in=bool(account),
            )

        try:
            result = answer(payload.question)
        except QuestionError as problem:
            # Nothing was answered, so the question doesn't count against the visitor.
            give_back_question(counter(account), asked_at)
            status, retry_after = QUESTION_ERRORS.get(problem.code, (500, None))
            return error(problem.code, str(problem), status, retry_after)
        return jsonify({"answer": result.text, "sources": result.sources,
                        "warning": result.warning, "model": result.model, "remaining": left})

    @app.get("/api/account")
    def account_status():
        with connect_limits() as conn:
            account = accounts.session_account(conn, session_token(), now())
            per_hour, per_day = allowance(account)
            left = limits.remaining(conn, counter(account), now(), per_hour, per_day)
        return jsonify({
            # Only a confirmed address can have a session, so signed in also means verified.
            "signed_in": bool(account), "email": account[1] if account else None,
            "limits": {"hour": per_hour, "day": per_day}, "remaining": left,
        })

    def give_back_question(who: str, asked_at: datetime) -> None:
        try:
            with connect_limits() as conn:
                limits.give_back_question(conn, who, asked_at)
        except psycopg.Error as problem:
            log.error("A failed question could not be given back: %s", problem)

    def read_address(model):
        """(payload, email, None) from the request, or (None, None, the error response)."""
        payload, refused = read_json(model, "an email address")
        if refused:
            return None, None, refused
        try:
            return payload, accounts.normalize_email(payload.email), None
        except ValueError:
            return None, None, error("invalid_email", "Please type a valid email address.", 400)

    def link_to(path: str, token: str) -> str:
        base = config.PUBLIC_API_URL or request.host_url.rstrip("/")
        return f"{base}{path}?token={quote(token)}"

    def mail_is_set_up() -> bool:
        return bool(config.RESEND_API_KEY or config.EMAIL_LOG_LINKS)

    def sent(message: str):
        # The same answer for every address, known or not.
        response = jsonify({"status": "sent", "message": message})
        response.status_code = 202
        return response

    def mail_failed(problem: Exception):
        log.error("Email not sent: %s", problem)
        return error("sign_in_unavailable", "The email could not be sent. Please try again later.", 503, 60)

    @app.post("/api/sign-up")
    def sign_up():
        payload, email, refused = read_address(AddressAndPassword)
        if refused:
            return refused
        try:
            accounts.check_new_password(payload.password)
        except ValueError as problem:
            return error("invalid_password", str(problem), 400)
        if accounts.is_disposable(email):
            return error("disposable_email", "Please use a permanent email address. "
                         "Temporary inbox services can't be used to sign up.", 400)
        if not mail_is_set_up():
            return error("sign_in_unavailable", "Signing up is not available yet.", 503)
        refused = human_check(payload.captcha_token)
        if refused:
            return refused
        with connect_limits() as conn:
            limits.take_mail(conn, hashed(email), hashed(visitor_ip()), now())
            token = accounts.sign_up(conn, email, payload.password, now())
        try:
            if token:
                mail.send_confirmation(email, link_to("/api/sign-up/confirm", token))
            else:
                mail.send_already_registered(email)
        except mailer.MailUnavailable as problem:
            return mail_failed(problem)
        return sent("Check your inbox: we sent you a confirmation link. It works for 24 hours.")

    @app.get("/api/sign-up/confirm")
    def confirm_page():
        token = request.args.get("token", "")[:200]
        try:
            with connect_limits() as conn:
                email = accounts.link_email(conn, token, accounts.CONFIRM, now()) if token else None
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return unavailable_page()
        if not email:
            return link_used_page()
        nonce = accounts.new_token()
        return with_nonce(page("Confirm your email", (
            f"<p>Confirm <strong>{escape(email)}</strong> for the question box on adnsaudio.com?</p>"
            '<form method="post" action="/api/sign-up/confirm">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            f'<input type="hidden" name="nonce" value="{nonce}">'
            "<button type=\"submit\">Confirm my email</button></form>"
            "<p>If you didn't sign up, close this page; nothing happens.</p>"
        )), nonce, "/api/sign-up/confirm", accounts.LINK_LIFETIME[accounts.CONFIRM])

    @app.post("/api/sign-up/confirm")
    def confirm():
        if not nonce_matches():
            return page("Please open the link again", (
                "<p>Open the confirmation link from your email again, then press the button.</p>"), 403)
        try:
            with connect_limits() as conn:
                confirmed = accounts.confirm_email(conn, request.form.get("token", "")[:200], now())
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return unavailable_page()
        if not confirmed:
            return link_used_page()
        return back_to_the_site("verified", confirmed[0], "/api/sign-up/confirm")

    @app.post("/api/sign-in")
    def sign_in():
        payload, email, refused = read_address(AddressAndPassword)
        if refused:
            return refused
        refused = human_check(payload.captcha_token)
        if refused:
            return refused
        try:
            with connect_limits() as conn:
                # Counted before the password is checked, right or wrong, so guessing is slow.
                limits.take_sign_in(conn, hashed(email), hashed(visitor_ip()), now())
                signed_in = accounts.sign_in(conn, email, payload.password, now())
        except accounts.NotConfirmed:
            return error("email_not_confirmed", "Please confirm your email first, with the link we sent you. "
                         "If you can't find it, sign up again for a new one.", 403)
        if not signed_in:
            # The same answer for an unknown address and a wrong password.
            return error("invalid_credentials", "That email and password don't match.", 401)
        return with_session(jsonify({"status": "signed_in", "email": email}), signed_in[0])

    @app.post("/api/password-reset")
    def ask_for_reset():
        payload, email, refused = read_address(ResetRequest)
        if refused:
            return refused
        if not mail_is_set_up():
            return error("sign_in_unavailable", "Password resets are not available yet.", 503)
        refused = human_check(payload.captcha_token)
        if refused:
            return refused
        with connect_limits() as conn:
            limits.take_mail(conn, hashed(email), hashed(visitor_ip()), now())
            token = accounts.ask_for_reset(conn, email, now())
        try:
            if token:
                mail.send_reset(email, link_to("/api/password-reset/confirm", token))
        except mailer.MailUnavailable as problem:
            return mail_failed(problem)
        return sent("If that address has an account, a new-password link is on its way. It works for an hour.")

    def reset_form(email: str, token: str, problem: str = None, status: int = 200):
        nonce = accounts.new_token()
        note = f'<p class="problem">{escape(problem)}</p>' if problem else ""
        response = page("Choose a new password", (
            f"<p>For <strong>{escape(email)}</strong>. All other devices are signed out.</p>{note}"
            '<form method="post" action="/api/password-reset/confirm">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            f'<input type="hidden" name="nonce" value="{nonce}">'
            # Tells password managers which account the new password belongs to.
            f'<input type="email" name="username" autocomplete="username" value="{escape(email)}" readonly hidden>'
            f'<label for="password">New password, at least {accounts.MIN_PASSWORD_LENGTH} characters</label>'
            '<input id="password" type="password" name="password" autocomplete="new-password" required '
            f'minlength="{accounts.MIN_PASSWORD_LENGTH}" maxlength="{accounts.MAX_PASSWORD_LENGTH}">'
            '<button type="submit">Change my password</button></form>'
        ), status)
        return with_nonce(response, nonce, "/api/password-reset/confirm", accounts.LINK_LIFETIME[accounts.RESET])

    @app.get("/api/password-reset/confirm")
    def reset_page():
        token = request.args.get("token", "")[:200]
        try:
            with connect_limits() as conn:
                email = accounts.link_email(conn, token, accounts.RESET, now()) if token else None
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return unavailable_page()
        return reset_form(email, token) if email else link_used_page()

    @app.post("/api/password-reset/confirm")
    def reset():
        if not nonce_matches():
            return page("Please open the link again", (
                "<p>Open the new-password link from your email again, then try once more.</p>"), 403)
        token = request.form.get("token", "")[:200]
        password = request.form.get("password", "")
        try:
            with connect_limits() as conn:
                try:
                    accounts.check_new_password(password)
                except ValueError as problem:
                    email = accounts.link_email(conn, token, accounts.RESET, now())
                    return reset_form(email, token, str(problem), 400) if email else link_used_page()
                changed = accounts.reset_password(conn, token, password, now())
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return unavailable_page()
        if not changed:
            return link_used_page()
        return back_to_the_site("password_changed", changed[0], "/api/password-reset/confirm")

    @app.post("/api/sign-out")
    def sign_out():
        if not request.is_json:
            return error("invalid_request", "Send a JSON request.", 400)
        if session_token():
            try:
                with connect_limits() as conn:
                    accounts.sign_out(conn, session_token())
            except psycopg.Error as problem:
                return database_down(problem)
        return without_session(jsonify({"status": "signed_out"}))

    @app.post("/api/account/delete")
    def delete_account():
        if not request.is_json:
            return error("invalid_request", "Send a JSON request.", 400)
        with connect_limits() as conn:
            account = accounts.session_account(conn, session_token(), now())
            if not account:
                return error("not_signed_in", "Sign in first to delete your account.", 401)
            accounts.delete_account(conn, account[0], counter(account))
        return without_session(jsonify({"status": "deleted"}))

    @app.post("/api/label-requests")
    def request_label():
        payload, refused = read_json(LabelRequest, "a label name")
        if refused:
            return refused
        try:
            name = label_requests.clean_name(payload.name)
        except ValueError as problem:
            return error("invalid_label_name", str(problem), 400)
        with connect_limits() as conn:
            account = accounts.session_account(conn, session_token(), now())
            if not account:
                return error("not_signed_in", "Sign in to request a label.", 401)
            # Read in full, or the request is refused: a failed read must not let a tracked
            # label be requested again. Labels still being imported count as tracked.
            known = label_requests.tracked_match(name, known_labels())
            if known:
                return jsonify({"status": "tracked", "message": f"{known} is already tracked. You can ask about it now."})
            new = label_requests.request_label(conn, account[0], name, now())
        if not new:
            return jsonify({"status": "requested", "message": f"You have already asked for {name}."})
        response = jsonify({"status": "requested",
                            "message": f"Thanks. {name} is waiting for review; requests are checked by hand."})
        response.status_code = 201
        return response

    @app.get("/api/label-requests")
    def my_label_requests():
        with connect_limits() as conn:
            account = accounts.session_account(conn, session_token(), now())
            if not account:
                return error("not_signed_in", "Sign in to see your label requests.", 401)
            return jsonify({"requests": label_requests.requests_of(conn, account[0])})

    @app.get("/api/labels")
    def labels():
        names = list_labels()
        response = jsonify({"labels": names})
        response.headers["Cache-Control"] = f"public, max-age={LABELS_CACHE_SECONDS}"
        return response

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

    @app.errorhandler(limits.LimitReached)
    def limit_reached(reached: limits.LimitReached):
        log.info("Refused: %s", reached.code)
        return error(reached.code, str(reached), 429, reached.retry_after)

    @app.errorhandler(psycopg.Error)
    def database_down(problem: psycopg.Error):
        # Without the database neither limits nor sign-in can be checked, so the request is refused.
        log.error("Web database unavailable: %s", problem)
        return error("database", "The question box is unavailable right now. Please try again later.", 503, 60)

    @app.errorhandler(HTTPException)
    def http_error(problem: HTTPException):
        messages = {404: "Not found.", 405: "Method not allowed.", 413: "The request is too large."}
        return error(problem.name.lower().replace(" ", "_"), messages.get(problem.code, problem.name + "."), problem.code)

    @app.errorhandler(Exception)
    def unexpected(problem: Exception):
        log.exception("Unexpected error in the web API")
        return error("internal_error", "Something went wrong. Please try again later.", 500)

    @app.after_request
    def headers(response):
        origin = request.headers.get("Origin")
        if origin and origin in config.ALLOWED_ORIGINS:
            response.headers["Access-Control-Allow-Origin"] = origin
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            # The page sends the session cookie with its requests.
            response.headers["Access-Control-Allow-Credentials"] = "true"
            # The page is on another origin, so it may only read this header if it is named here.
            response.headers["Access-Control-Expose-Headers"] = "Retry-After"
            response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Confirmation and new-password links carry their token in the address; never pass it on.
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.mimetype == "text/html":
            site = urlsplit(config.SIGN_IN_RETURN_URL)
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; "
                f"form-action 'self' {site.scheme}://{site.netloc}"
            )
        return response

    @app.after_request
    def write_access_log(response):
        # The path only: a query string could hold anything. The same hash as the question counts.
        visitor = hashed(visitor_ip())
        access_log.info("%s %s %s %s %s", visitor, request.method, for_the_log(request.path), response.status_code,
                        for_the_log(request.headers.get("User-Agent", "-")))
        return response

    return app
