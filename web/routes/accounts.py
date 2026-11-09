"""Accounts (the rules are in web/accounts.py). Accounts can't be used until the
address is confirmed:
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
import logging
from urllib.parse import quote

import psycopg
from flask import Blueprint, jsonify, request
from markupsafe import escape
from pydantic import BaseModel, ConfigDict, Field

from web import accounts, config, limits, mailer
from web.routes.pages import (back_to_the_site, link_used_page, nonce_matches, page, unavailable_page,
                              with_nonce, with_session, without_session)
from web.routes.shared import (CAPTCHA_TOKEN_LENGTH, allowance, counter, database_down, error, hashed,
                               human_check, read_json, services, session_token, visitor_ip)

log = logging.getLogger(__name__)

blueprint = Blueprint("accounts", __name__)

# Room for spaces typed around an address; accounts.normalize_email trims them and
# then applies the real limit.
EMAIL_FIELD_LENGTH = accounts.MAX_EMAIL_LENGTH + 20


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


@blueprint.get("/api/account")
def account_status():
    outside = services()
    with outside.connect_limits() as conn:
        account = accounts.session_account(conn, session_token(), outside.now())
        per_hour, per_day = allowance(account)
        left = limits.remaining(conn, counter(account), outside.now(), per_hour, per_day)
    return jsonify({
        # Only a confirmed address can have a session, so signed in also means verified.
        "signed_in": bool(account), "email": account[1] if account else None,
        "limits": {"hour": per_hour, "day": per_day}, "remaining": left,
    })


@blueprint.post("/api/sign-up")
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
    outside = services()
    with outside.connect_limits() as conn:
        limits.take_mail(conn, hashed(email), hashed(visitor_ip()), outside.now())
        token = accounts.sign_up(conn, email, payload.password, outside.now())
    try:
        if token:
            outside.mail.send_confirmation(email, link_to("/api/sign-up/confirm", token))
        else:
            outside.mail.send_already_registered(email)
    except mailer.MailUnavailable as problem:
        return mail_failed(problem)
    return sent("Check your inbox: we sent you a confirmation link. It works for 24 hours.")


@blueprint.get("/api/sign-up/confirm")
def confirm_page():
    outside = services()
    token = request.args.get("token", "")[:200]
    try:
        with outside.connect_limits() as conn:
            email = accounts.link_email(conn, token, accounts.CONFIRM, outside.now()) if token else None
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


@blueprint.post("/api/sign-up/confirm")
def confirm():
    if not nonce_matches():
        return page("Please open the link again", (
            "<p>Open the confirmation link from your email again, then press the button.</p>"), 403)
    outside = services()
    try:
        with outside.connect_limits() as conn:
            confirmed = accounts.confirm_email(conn, request.form.get("token", "")[:200], outside.now())
    except psycopg.Error as problem:
        log.error("Web database unavailable: %s", problem)
        return unavailable_page()
    if not confirmed:
        return link_used_page()
    return back_to_the_site("verified", confirmed[0], "/api/sign-up/confirm")


@blueprint.post("/api/sign-in")
def sign_in():
    payload, email, refused = read_address(AddressAndPassword)
    if refused:
        return refused
    refused = human_check(payload.captcha_token)
    if refused:
        return refused
    outside = services()
    try:
        with outside.connect_limits() as conn:
            # Counted before the password is checked, right or wrong, so guessing is slow.
            limits.take_sign_in(conn, hashed(email), hashed(visitor_ip()), outside.now())
            signed_in = accounts.sign_in(conn, email, payload.password, outside.now())
    except accounts.NotConfirmed:
        return error("email_not_confirmed", "Please confirm your email first, with the link we sent you. "
                     "If you can't find it, sign up again for a new one.", 403)
    if not signed_in:
        # The same answer for an unknown address and a wrong password.
        return error("invalid_credentials", "That email and password don't match.", 401)
    return with_session(jsonify({"status": "signed_in", "email": email}), signed_in[0])


@blueprint.post("/api/password-reset")
def ask_for_reset():
    payload, email, refused = read_address(ResetRequest)
    if refused:
        return refused
    if not mail_is_set_up():
        return error("sign_in_unavailable", "Password resets are not available yet.", 503)
    refused = human_check(payload.captcha_token)
    if refused:
        return refused
    outside = services()
    with outside.connect_limits() as conn:
        limits.take_mail(conn, hashed(email), hashed(visitor_ip()), outside.now())
        token = accounts.ask_for_reset(conn, email, outside.now())
    try:
        if token:
            outside.mail.send_reset(email, link_to("/api/password-reset/confirm", token))
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


@blueprint.get("/api/password-reset/confirm")
def reset_page():
    outside = services()
    token = request.args.get("token", "")[:200]
    try:
        with outside.connect_limits() as conn:
            email = accounts.link_email(conn, token, accounts.RESET, outside.now()) if token else None
    except psycopg.Error as problem:
        log.error("Web database unavailable: %s", problem)
        return unavailable_page()
    return reset_form(email, token) if email else link_used_page()


@blueprint.post("/api/password-reset/confirm")
def reset():
    if not nonce_matches():
        return page("Please open the link again", (
            "<p>Open the new-password link from your email again, then try once more.</p>"), 403)
    outside = services()
    token = request.form.get("token", "")[:200]
    password = request.form.get("password", "")
    try:
        with outside.connect_limits() as conn:
            try:
                accounts.check_new_password(password)
            except ValueError as problem:
                email = accounts.link_email(conn, token, accounts.RESET, outside.now())
                return reset_form(email, token, str(problem), 400) if email else link_used_page()
            changed = accounts.reset_password(conn, token, password, outside.now())
    except psycopg.Error as problem:
        log.error("Web database unavailable: %s", problem)
        return unavailable_page()
    if not changed:
        return link_used_page()
    return back_to_the_site("password_changed", changed[0], "/api/password-reset/confirm")


@blueprint.post("/api/sign-out")
def sign_out():
    if not request.is_json:
        return error("invalid_request", "Send a JSON request.", 400)
    if session_token():
        try:
            with services().connect_limits() as conn:
                accounts.sign_out(conn, session_token())
        except psycopg.Error as problem:
            return database_down(problem)
    return without_session(jsonify({"status": "signed_out"}))


@blueprint.post("/api/account/delete")
def delete_account():
    if not request.is_json:
        return error("invalid_request", "Send a JSON request.", 400)
    outside = services()
    with outside.connect_limits() as conn:
        account = accounts.session_account(conn, session_token(), outside.now())
        if not account:
            return error("not_signed_in", "Sign in first to delete your account.", 401)
        accounts.delete_account(conn, account[0], counter(account))
    return without_session(jsonify({"status": "deleted"}))
