"""The emails behind signing up, sent through Resend's HTTP API.

Without RESEND_API_KEY, links are written to the log instead when EMAIL_LOG_LINKS
is true (for trying it on a laptop), and otherwise signing up is switched off: the
API says so rather than pretending to send.

The logo travels inside the email (an inline attachment), because many mail apps
block images fetched from the web.
"""
import base64
import logging
from pathlib import Path

import requests
from markupsafe import escape

from web import config

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT = 10
LOGO_FILE = Path(__file__).parent / "assets" / "adns-audio-logo.png"
LOGO_ID = "adns-audio-logo"

log = logging.getLogger(__name__)


class MailUnavailable(Exception):
    """The email could not be sent, or sending is not set up."""


def html_message(title: str, paragraphs: list, button: tuple = None) -> str:
    """Everything passed in is escaped. button is (text, link)."""
    body = "".join(f'<p style="margin:0 0 16px">{escape(text)}</p>' for text in paragraphs)
    action = ""
    if button:
        text, link = button
        action = (
            f'<p style="margin:24px 0"><a href="{escape(link)}" style="display:inline-block;padding:12px 22px;'
            f'background:#392850;border:1px solid #b19bdd;color:#eceef2;font-weight:600;text-decoration:none">{escape(text)}</a></p>'
            f'<p style="margin:0 0 16px;font-size:13px;color:#9aa0ad">If the button doesn\'t work, copy this address into your browser:<br>'
            f'<a href="{escape(link)}" style="color:#b19bdd;word-break:break-all">{escape(link)}</a></p>'
        )
    return (
        '<div style="background:#07080b;padding:32px 16px"><div style="max-width:480px;margin:0 auto;padding:28px;'
        'background:#111318;border:1px solid #403553;color:#c9cdd6;font:16px/1.6 system-ui,-apple-system,Segoe UI,sans-serif">'
        f'<h1 style="margin:0 0 16px;color:#eceef2;font-size:22px">{escape(title)}</h1>{body}{action}'
        f'<img src="cid:{LOGO_ID}" alt="ADNS Audio" width="80" height="80" style="display:block;border:0;margin:24px 0 0">'
        '</div></div>'
    )


def send(email: str, subject: str, text: str, html: str, link: str = None) -> None:
    if not config.RESEND_API_KEY:
        if config.EMAIL_LOG_LINKS:
            log.warning("Email sending is not set up; %r for %s: %s", subject, email, link)
            return
        raise MailUnavailable("RESEND_API_KEY is not set")
    logo = {"filename": LOGO_FILE.name, "content": base64.b64encode(LOGO_FILE.read_bytes()).decode("ascii"),
            "content_id": LOGO_ID}
    try:
        response = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={"from": config.SIGN_IN_EMAIL_FROM, "to": [email], "subject": subject, "text": text, "html": html,
                  "attachments": [logo]},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        # The response may echo the request, so only the status goes to the log.
        status = getattr(error.response, "status_code", None)
        raise MailUnavailable(f"Resend did not accept the email (status {status})") from error


def send_confirmation(email: str, link: str) -> None:
    title = "Confirm your email"
    lead = "Thanks for signing up to Market Trends on adnsaudio.com. Confirm your email address to finish and return to the question box."
    note = "The link works once, within 24 hours. If you didn't sign up, ignore this email and the account is deleted after 7 days."
    send(email, "Confirm your email for Market Trends", f"{lead}\n\n{link}\n\n{note}\n",
         html_message(title, [lead, note], ("Confirm my email", link)), link)


def send_reset(email: str, link: str) -> None:
    title = "Choose a new password"
    lead = "Someone asked to change the password of your Market Trends account on adnsaudio.com."
    note = "The link works once, within an hour. If it wasn't you, ignore this email; your password stays the same."
    send(email, "Choose a new password for Market Trends", f"{lead}\n\n{link}\n\n{note}\n",
         html_message(title, [lead, note], ("Choose a new password", link)), link)


def send_already_registered(email: str) -> None:
    """Told only to the mailbox, so the page gives nobody a way to find out who has an account."""
    title = "You already have an account"
    lead = "Someone tried to sign up to Market Trends on adnsaudio.com with this address, which already has an account."
    note = "If it was you, sign in on the Market Trends page; \"Forgot your password?\" is there too. If it wasn't, ignore this email."
    send(email, "You already have a Market Trends account", f"{lead}\n\n{note}\n\n{config.SIGN_IN_RETURN_URL}\n",
         html_message(title, [lead, note], ("Go to Market Trends", config.SIGN_IN_RETURN_URL)), config.SIGN_IN_RETURN_URL)


def send_label_request(reviewer: str, request_id: int, name: str, accounts: int, matches: list) -> None:
    """Tell the reviewer about a new request. matches: [(discogs id, name, entries)], best first."""
    asked = f"{accounts} {'person has' if accounts == 1 else 'people have'} asked for it."
    if matches:
        found = [f"{label_id}  {title}  ({entries} entries)  https://www.discogs.com/label/{label_id}"
                 for label_id, title, entries in matches]
    else:
        found = ["Discogs found no match, or could not be reached. Find the label on discogs.com: "
                 "its id is the number in the address."]
    commands = [f"sh scripts/label-requests review {request_id}",
                f"sh scripts/label-requests approve {request_id} <discogs id or link>",
                f'sh scripts/label-requests reject {request_id} "the reason"']
    lead = f"A visitor asked Market Trends to track {name!r} (request {request_id}). {asked}"
    text = "\n".join([lead, "", "Labels on Discogs with that name:", *found, "", "To decide, from the project folder:",
                      *commands, ""])
    send(reviewer, f"Label request: {name}", text,
         html_message(f"Label request: {name}", [lead, "Labels on Discogs with that name:", *found,
                                                 "To decide, from the project folder:", *commands]))


def send_label_rejected(email: str, name: str, reason: str) -> None:
    lead = f"Thanks for asking Market Trends to track {name}. We won't add it, for this reason:"
    note = "You can ask about the labels we do track on the Market Trends page."
    send(email, f"About your request for {name}", f"{lead}\n\n{reason}\n\n{note}\n\n{config.SIGN_IN_RETURN_URL}\n",
         html_message(f"About your request for {name}", [lead, reason, note],
                      ("Go to Market Trends", config.SIGN_IN_RETURN_URL)),
         config.SIGN_IN_RETURN_URL)
