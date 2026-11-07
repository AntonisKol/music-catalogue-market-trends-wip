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

Sign-in with a username and password (see web/accounts.py). Accounts are created
only with web/manage_accounts.py, so there is no way to register here:
    POST /api/sign-in            {"username", "password", "captcha_token"}: starts a session
    GET  /api/account            signed in or not, and the questions left
    POST /api/sign-out           ends this session
    POST /api/account/delete     deletes the account and everything stored for it
"""
import logging
from datetime import datetime, timezone

import psycopg
from flask import Flask, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from queries.questions import MAX_QUESTION_LENGTH, QuestionError, answer_question
from web import accounts, captcha, config, limits

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


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    # Not needed when signed in.
    captcha_token: str = Field(default="", max_length=2048)


class SignInRequest(BaseModel):
    # Spaces are not stripped: they can be part of a password.
    model_config = ConfigDict(extra="forbid")

    username: str = Field(min_length=1, max_length=accounts.MAX_USERNAME_LENGTH)
    password: str = Field(min_length=1, max_length=accounts.MAX_PASSWORD_LENGTH)
    captcha_token: str = Field(min_length=1, max_length=2048)


def error(code: str, message: str, status: int, retry_after: int = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def create_app(answer=answer_question, verify_captcha=captcha.verify, connect_limits=limits.connect_limits,
               now=lambda: datetime.now(timezone.utc)) -> Flask:
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
            "signed_in": bool(account), "username": account[1] if account else None,
            "limits": {"hour": per_hour, "day": per_day}, "remaining": left,
        })

    @app.post("/api/sign-in")
    def sign_in():
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return error("invalid_request", "Send a JSON object with a username and a password.", 400)
        try:
            payload = SignInRequest.model_validate(body)
        except ValidationError as problem:
            fields = sorted({str(e["loc"][0]) for e in problem.errors() if e["loc"]})
            return error("invalid_request", f"Invalid request: {', '.join(fields) or 'body'}.", 400)
        refused = human_check(payload.captcha_token)
        if refused:
            return refused
        try:
            with connect_limits() as conn:
                # Counted before the password is checked, right or wrong, so guessing is slow.
                limits.take_sign_in(conn, accounts.username_key(payload.username, config.VISITOR_HASH_SECRET),
                                    limits.visitor_id(visitor_ip(), config.VISITOR_HASH_SECRET), now())
                signed_in = accounts.sign_in(conn, payload.username, payload.password, now())
        except limits.LimitReached as reached:
            return error(reached.code, str(reached), 429, reached.retry_after)
        except psycopg.Error as problem:
            return database_down(problem)
        if not signed_in:
            # The same answer for an unknown username and a wrong password.
            return error("invalid_credentials", "That username and password don't match.", 401)
        session, _account_id, username = signed_in
        response = jsonify({"status": "signed_in", "username": username})
        response.set_cookie(SESSION_COOKIE, session, max_age=int(accounts.SESSION_LIFETIME.total_seconds()),
                            path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
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
        response.headers["Referrer-Policy"] = "no-referrer"
        return response

    return app
