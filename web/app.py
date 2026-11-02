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

Email sign-in (see web/accounts.py):
    POST /api/sign-in            {"email", "captcha_token"}: emails a one-time link
    GET  /api/sign-in/confirm    the link: a page with a confirm button
    POST /api/sign-in/confirm    the button: starts a session, back to the site
    GET  /api/account            signed in or not, and the questions left
    POST /api/sign-out           ends this session
    POST /api/account/delete     deletes the account and everything stored for it
"""
import hmac
import logging
from datetime import datetime, timezone
from urllib.parse import quote, urlsplit

import psycopg
from flask import Flask, jsonify, make_response, redirect, request
from markupsafe import escape
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from queries.questions import MAX_QUESTION_LENGTH, QuestionError, answer_question
from web import accounts, captcha, config, limits, mailer

log = logging.getLogger(__name__)

MAX_REQUEST_BYTES = 8 * 1024

# Question service errors: HTTP status and, for temporary ones, seconds to wait.
QUESTION_ERRORS = {
    "invalid_question": (400, None),
    "busy": (503, 60),
    "unavailable": (503, 60),
    "database": (503, 60),
    "configuration": (500, None),
}


SESSION_COOKIE = "mt_session"
CONFIRM_COOKIE = "mt_confirm"


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not needed when signed in.
    captcha_token: str = Field(default="", max_length=2048)


class SignInRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    email: str = Field(min_length=3, max_length=accounts.MAX_EMAIL_LENGTH)
    captcha_token: str = Field(min_length=1, max_length=2048)


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
        "<p>Sign-in links work once, within 15 minutes. This one has expired or was already used.</p>"
        f'<p><a href="{escape(config.SIGN_IN_RETURN_URL)}">Back to the question box</a> to ask for a new one.</p>'
    ), 400)


def error(code: str, message: str, status: int, retry_after: int = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def create_app(answer=answer_question, verify_captcha=captcha.verify, connect_limits=limits.connect_limits,
               send_link=mailer.send_sign_in_link, now=lambda: datetime.now(timezone.utc)) -> Flask:
    """The app. The arguments exist so tests can replace the outside services."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if config.TRUSTED_PROXIES:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUSTED_PROXIES)

    def visitor_ip() -> str:
        return request.remote_addr or "unknown"

    def session_token() -> str:
        return request.cookies.get(SESSION_COOKIE, "")

    def counter(account) -> str:
        return f"account:{account[0]}" if account else limits.visitor_id(visitor_ip(), config.VISITOR_HASH_SECRET)

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

    def database_down(problem: Exception):
        # Without the database neither limits nor sign-in can be checked, so the request is refused.
        log.error("Web database unavailable: %s", problem)
        return error("database", "The question box is unavailable right now. Please try again later.", 503, 60)

    def without_session(response):
        response.delete_cookie(SESSION_COOKIE, path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
        return response

    @app.post("/api/questions")
    def ask():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("invalid_request", "Send a JSON object with a question.", 400)
        try:
            payload = QuestionRequest.model_validate(body)
        except ValidationError as problem:
            fields = sorted({str(e["loc"][0]) for e in problem.errors() if e["loc"]})
            if "question" in fields:
                return error("invalid_question", f"Please type a question of up to {MAX_QUESTION_LENGTH} characters.", 400)
            return error("invalid_request", f"Invalid request: {', '.join(fields) or 'body'}.", 400)

        try:
            with connect_limits() as conn:
                account = accounts.session_account(conn, session_token(), now())
                if not account:
                    refused = human_check(payload.captcha_token)
                    if refused:
                        return refused
                per_hour, per_day = allowance(account)
                left = limits.take_question(
                    conn, counter(account), now(), per_hour, per_day, config.QUESTIONS_PER_SITE_DAY,
                    signed_in=bool(account),
                )
        except limits.LimitReached as reached:
            log.info("Question refused: %s", reached.code)
            return error(reached.code, str(reached), 429, reached.retry_after)
        except psycopg.Error as problem:
            return database_down(problem)

        try:
            result = answer(payload.question)
        except QuestionError as problem:
            status, retry_after = QUESTION_ERRORS.get(problem.code, (500, None))
            return error(problem.code, str(problem), status, retry_after)
        return jsonify({"answer": result.text, "sources": result.sources,
                        "warning": result.warning, "model": result.model, "remaining": left})

    @app.get("/api/account")
    def account_status():
        try:
            with connect_limits() as conn:
                account = accounts.session_account(conn, session_token(), now())
                per_hour, per_day = allowance(account)
                left = limits.remaining(conn, counter(account), now(), per_hour, per_day)
        except psycopg.Error as problem:
            return database_down(problem)
        return jsonify({
            "signed_in": bool(account), "email": account[1] if account else None,
            "limits": {"hour": per_hour, "day": per_day}, "remaining": left,
        })

    @app.post("/api/sign-in")
    def ask_for_link():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("invalid_request", "Send a JSON object with an email address.", 400)
        try:
            payload = SignInRequest.model_validate(body)
        except ValidationError as problem:
            fields = sorted({str(e["loc"][0]) for e in problem.errors() if e["loc"]})
            if "email" in fields:
                return error("invalid_email", "Please type a valid email address.", 400)
            return error("invalid_request", f"Invalid request: {', '.join(fields) or 'body'}.", 400)
        try:
            email = accounts.normalize_email(payload.email)
        except ValueError:
            return error("invalid_email", "Please type a valid email address.", 400)
        if not (config.RESEND_API_KEY or config.SIGN_IN_LOG_LINKS):
            return error("sign_in_unavailable", "Signing in is not available yet.", 503)
        refused = human_check(payload.captcha_token)
        if refused:
            return refused
        try:
            with connect_limits() as conn:
                limits.take_sign_in(conn, accounts.email_key(email, config.VISITOR_HASH_SECRET),
                                    limits.visitor_id(visitor_ip(), config.VISITOR_HASH_SECRET), now())
                token = accounts.create_sign_in_link(conn, email, now())
        except limits.LimitReached as reached:
            return error(reached.code, str(reached), 429, reached.retry_after)
        except psycopg.Error as problem:
            return database_down(problem)
        base = config.PUBLIC_API_URL or request.host_url.rstrip("/")
        try:
            send_link(email, f"{base}/api/sign-in/confirm?token={quote(token)}")
        except mailer.MailUnavailable as problem:
            log.error("Sign-in email not sent: %s", problem)
            return error("sign_in_unavailable", "The sign-in email could not be sent. Please try again later.", 503, 60)
        # The same answer for every address, known or not.
        response = jsonify({"status": "sent", "message": "A sign-in link is on its way to that address. "
                            "It works once, within 15 minutes."})
        response.status_code = 202
        return response

    @app.get("/api/sign-in/confirm")
    def confirm_page():
        token = request.args.get("token", "")[:200]
        try:
            with connect_limits() as conn:
                email = accounts.link_email(conn, token, now()) if token else None
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return page("Sign-in is unavailable", "<p>Please try again in a minute.</p>", 503)
        if not email:
            return link_used_page()
        # A random value both in a cookie and in the form: a form posted from
        # another site has no way to carry the cookie, so it is refused.
        nonce = accounts.new_token()
        response = page("Sign in to Market Trends", (
            f"<p>Sign in to the question box on adnsaudio.com as <strong>{escape(email)}</strong>?</p>"
            '<form method="post" action="/api/sign-in/confirm">'
            f'<input type="hidden" name="token" value="{escape(token)}">'
            f'<input type="hidden" name="nonce" value="{nonce}">'
            "<button type=\"submit\">Sign in</button></form>"
            "<p>If you didn't ask for this, close this page; nothing happens.</p>"
        ))
        response.set_cookie(CONFIRM_COOKIE, nonce, max_age=int(accounts.LINK_LIFETIME.total_seconds()),
                            path="/api/sign-in", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
        return response

    @app.post("/api/sign-in/confirm")
    def confirm():
        token = request.form.get("token", "")[:200]
        nonce = request.form.get("nonce", "")
        if not nonce or not hmac.compare_digest(nonce, request.cookies.get(CONFIRM_COOKIE, "")):
            return page("Please open the link again", (
                "<p>Open the sign-in link from your email again, then press Sign in.</p>"), 403)
        try:
            with connect_limits() as conn:
                signed_in = accounts.sign_in(conn, token, now())
        except psycopg.Error as problem:
            log.error("Web database unavailable: %s", problem)
            return page("Sign-in is unavailable", "<p>Please open the link again in a minute.</p>", 503)
        if not signed_in:
            return link_used_page()
        session, _account_id, _email = signed_in
        response = redirect(config.SIGN_IN_RETURN_URL, 303)
        response.set_cookie(SESSION_COOKIE, session, max_age=int(accounts.SESSION_LIFETIME.total_seconds()),
                            path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
        response.delete_cookie(CONFIRM_COOKIE, path="/api/sign-in", secure=config.COOKIE_SECURE,
                               httponly=True, samesite="Strict")
        return response

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
        try:
            with connect_limits() as conn:
                account = accounts.session_account(conn, session_token(), now())
                if not account:
                    return error("not_signed_in", "Sign in first to delete your account.", 401)
                accounts.delete_account(conn, account[0], counter(account))
        except psycopg.Error as problem:
            return database_down(problem)
        return without_session(jsonify({"status": "deleted"}))

    @app.get("/api/health")
    def health():
        return jsonify({"status": "ok"})

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
            response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        # Sign-in links carry their token in the address; never pass it on.
        response.headers["Referrer-Policy"] = "no-referrer"
        if response.mimetype == "text/html":
            site = urlsplit(config.SIGN_IN_RETURN_URL)
            response.headers["Content-Security-Policy"] = (
                "default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; frame-ancestors 'none'; "
                f"form-action 'self' {site.scheme}://{site.netloc}"
            )
        return response

    return app
