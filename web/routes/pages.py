"""The small HTML pages that email links open, and the cookies they set.

Confirmation and new-password links open a page with a button or a form instead of
acting at once, so a mail scanner that follows links can't use them. Each form
carries a nonce that must match a cookie, and success sends the visitor back to the
question box, signed in.
"""
import hmac

from flask import make_response, redirect, request
from markupsafe import escape

from web import accounts, config
from web.routes.shared import SESSION_COOKIE

CONFIRM_COOKIE = "mt_confirm"

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


def without_session(response):
    response.delete_cookie(SESSION_COOKIE, path="/api", secure=config.COOKIE_SECURE, httponly=True, samesite="Strict")
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
