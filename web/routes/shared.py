"""What the routes share: the outside services, error answers, reading a JSON body,
who is asking, and the Turnstile human check.

create_app stores the services in app.extensions, so each blueprint reads them with
services() instead of from module globals, and tests can replace them per app.
"""
import logging
from dataclasses import dataclass
from typing import Callable

from flask import current_app, jsonify, request
from pydantic import ValidationError

from queries.questions import MAX_QUESTION_LENGTH
from web import captcha, config, limits

log = logging.getLogger(__name__)

SESSION_COOKIE = "mt_session"
CAPTCHA_TOKEN_LENGTH = 2048
LOGGED_TEXT_LENGTH = 200

# A field that fails validation gets its own message; any other gets a general one.
FIELD_ERRORS = {
    "question": ("invalid_question", f"Please type a question of up to {MAX_QUESTION_LENGTH} characters."),
    "email": ("invalid_email", "Please type a valid email address."),
    "password": ("invalid_password", "Please type a password."),
}


@dataclass(frozen=True)
class Services:
    """The outside services, given to create_app so tests can replace them."""
    answer: Callable
    verify_captcha: Callable
    connect_limits: Callable
    mail: object
    now: Callable
    list_labels: Callable
    known_labels: Callable
    dates: Callable


def services() -> Services:
    return current_app.extensions["market_trends"]


def error(code: str, message: str, status: int, retry_after: int = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def database_down(problem: Exception):
    # Without the database neither limits nor sign-in can be checked, so the request is refused.
    log.error("Web database unavailable: %s", problem)
    return error("database", "The question box is unavailable right now. Please try again later.", 503, 60)


def for_the_log(text: str) -> str:
    """Visitors choose the path and the browser name, so they can't be allowed to fake a log line."""
    return text[:LOGGED_TEXT_LENGTH].encode("unicode_escape").decode("ascii")


def visitor_ip() -> str:
    return request.remote_addr or "unknown"


def hashed(text: str) -> str:
    return limits.keyed_hash(text, config.VISITOR_HASH_SECRET)


def session_token() -> str:
    return request.cookies.get(SESSION_COOKIE, "")


def counter(account) -> str:
    """Whose questions are counted: the account's, or the visitor's hashed address."""
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
        human = services().verify_captcha(token, config.TURNSTILE_SECRET_KEY, visitor_ip())
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
