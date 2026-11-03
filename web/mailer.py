"""Sending sign-in links by email, through Resend's HTTP API.

Without RESEND_API_KEY, links are written to the log instead when
SIGN_IN_LOG_LINKS is true (for trying sign-in on a laptop), and otherwise
sign-in is switched off: the API says so rather than pretending to send.
"""
import logging

import requests

from web import config

RESEND_URL = "https://api.resend.com/emails"
TIMEOUT = 10

log = logging.getLogger(__name__)


class MailUnavailable(Exception):
    """The link could not be sent, or sending is not set up."""


def message(link: str) -> tuple:
    subject = "Your Market Trends sign-in link"
    text = (
        "Open this link to sign in to the Market Trends question box on adnsaudio.com:\n\n"
        f"{link}\n\n"
        "It works once, within 15 minutes. If you didn't ask for it, ignore this email; "
        "nothing happens unless the link is opened and confirmed.\n"
    )
    return subject, text


def send_sign_in_link(email: str, link: str) -> None:
    subject, text = message(link)
    if not config.RESEND_API_KEY:
        if config.SIGN_IN_LOG_LINKS:
            log.warning("Email sending is not set up; sign-in link for %s: %s", email, link)
            return
        raise MailUnavailable("RESEND_API_KEY is not set")
    try:
        response = requests.post(
            RESEND_URL,
            headers={"Authorization": f"Bearer {config.RESEND_API_KEY}"},
            json={"from": config.SIGN_IN_EMAIL_FROM, "to": [email], "subject": subject, "text": text},
            timeout=TIMEOUT,
        )
        response.raise_for_status()
    except requests.RequestException as error:
        # The response may echo the request, so only the status goes to the log.
        status = getattr(error.response, "status_code", None)
        raise MailUnavailable(f"Resend did not accept the email (status {status})") from error
