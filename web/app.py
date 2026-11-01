"""The web API the question page calls.

    flask --app web.app run                          # development
    gunicorn --workers 2 --timeout 120 'web.app:create_app()'   # production

POST /api/questions with {"question": "...", "captcha_token": "..."} returns
{"answer", "sources", "warning", "model"}. Each question passes, in this order:
a check of the request, the Turnstile human check, the visitor's and the site's
limits, and only then the question service, so nothing reaches the Anthropic API
before all three guards have passed. Errors come back as
{"error": {"code", "message"}} with a fitting HTTP status and never contain
secrets or internal details; those go to the log.
"""
import logging
from datetime import datetime, timezone

import psycopg
from flask import Flask, jsonify, request
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from queries.questions import MAX_QUESTION_LENGTH, QuestionError, answer_question
from web import captcha, config, limits

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


class QuestionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    question: str = Field(min_length=1, max_length=MAX_QUESTION_LENGTH)
    captcha_token: str = Field(min_length=1, max_length=2048)


def error(code: str, message: str, status: int, retry_after: int = None):
    response = jsonify({"error": {"code": code, "message": message}})
    response.status_code = status
    if retry_after:
        response.headers["Retry-After"] = str(retry_after)
    return response


def create_app(answer=answer_question, verify_captcha=captcha.verify,
               connect_limits=limits.connect_limits, now=lambda: datetime.now(timezone.utc)) -> Flask:
    """The app. The arguments exist so tests can replace the outside services."""
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if config.TRUSTED_PROXIES:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUSTED_PROXIES)

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

        visitor_ip = request.remote_addr or "unknown"
        try:
            human = verify_captcha(payload.captcha_token, config.TURNSTILE_SECRET_KEY, visitor_ip)
        except captcha.CaptchaUnavailable as problem:
            log.error("Turnstile verification unavailable: %s", problem)
            return error("captcha_unavailable", "The human check is unavailable right now. Please try again.", 503, 30)
        if not human:
            return error("captcha_failed", "The human check failed. Please try again.", 403)

        try:
            with connect_limits() as conn:
                limits.take_question(
                    conn, limits.visitor_id(visitor_ip, config.VISITOR_HASH_SECRET), now(),
                    config.QUESTIONS_PER_VISITOR_HOUR, config.QUESTIONS_PER_VISITOR_DAY, config.QUESTIONS_PER_SITE_DAY,
                )
        except limits.LimitReached as reached:
            log.info("Question refused: %s", reached.code)
            return error(reached.code, str(reached), 429, reached.retry_after)
        except psycopg.Error as problem:
            # Without counts the limits cannot be enforced, so the question is refused.
            log.error("Question limits unavailable: %s", problem)
            return error("database", "The question box is unavailable right now. Please try again later.", 503, 60)

        try:
            result = answer(payload.question)
        except QuestionError as problem:
            status, retry_after = QUESTION_ERRORS.get(problem.code, (500, None))
            return error(problem.code, str(problem), status, retry_after)
        return jsonify({"answer": result.text, "sources": result.sources,
                        "warning": result.warning, "model": result.model})

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
            response.headers["Access-Control-Allow-Methods"] = "POST, OPTIONS"
            response.headers["Access-Control-Allow-Headers"] = "Content-Type"
            response.headers["Access-Control-Max-Age"] = "600"
        response.headers["Vary"] = "Origin"
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        return response

    return app
