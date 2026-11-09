"""The web API the question page calls.

    flask --app web.app run                          # development
    gunicorn --workers 2 --timeout 120 'web.app:create_app()'   # production

The routes live in web/routes/, one blueprint per part (see web/routes/__init__.py):
questions, accounts, label requests, and the label list, data dates and health
check. This file builds the app and adds what every answer shares. Errors come
back as {"error": {"code", "message"}} with a fitting HTTP status and never contain
secrets or internal details; those go to the log.

Each request is written to the access log with a keyed hash of the visitor's
address, never the address, so abuse can be spotted without keeping addresses.
"""
import logging
import sys
from datetime import datetime, timezone
from urllib.parse import urlsplit

import psycopg
from flask import Flask, request
from werkzeug.exceptions import HTTPException
from werkzeug.middleware.proxy_fix import ProxyFix

from queries.labels import data_dates, listed_label_names, tracked_label_names
from queries.questions import answer_question
from web import captcha, config, limits, mailer
from web.routes import accounts, label_requests, labels, questions
from web.routes.pages import CONFIRM_COOKIE
from web.routes.shared import SESSION_COOKIE, Services, database_down, error, for_the_log, hashed, visitor_ip

# The cookie names are still offered here, where they were before the routes moved.
__all__ = ["create_app", "CONFIRM_COOKIE", "SESSION_COOKIE"]

log = logging.getLogger(__name__)
access_log = logging.getLogger("web.access")

MAX_REQUEST_BYTES = 8 * 1024


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
               list_labels=listed_label_names, known_labels=tracked_label_names, dates=data_dates) -> Flask:
    """The app. The arguments exist so tests can replace the outside services."""
    set_up_logging()
    app = Flask(__name__)
    app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    if config.TRUSTED_PROXIES:
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUSTED_PROXIES)
    app.extensions["market_trends"] = Services(
        answer=answer, verify_captcha=verify_captcha, connect_limits=connect_limits, mail=mail, now=now,
        list_labels=list_labels, known_labels=known_labels, dates=dates,
    )
    for part in (questions, accounts, label_requests, labels):
        app.register_blueprint(part.blueprint)

    @app.errorhandler(limits.LimitReached)
    def limit_reached(reached: limits.LimitReached):
        log.info("Refused: %s", reached.code)
        return error(reached.code, str(reached), 429, reached.retry_after)

    app.register_error_handler(psycopg.Error, database_down)

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
