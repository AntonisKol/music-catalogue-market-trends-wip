"""Cloudflare Turnstile: the human check on every question.

The page shows Turnstile's widget, which gives the visitor a one-time token; the
API sends the token to Cloudflare with our secret key and accepts the question
only if Cloudflare confirms it. If Cloudflare cannot be reached, the question is
refused rather than let through.
"""
import logging

import requests

VERIFY_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
TIMEOUT = 10

log = logging.getLogger(__name__)


class CaptchaUnavailable(Exception):
    """Cloudflare could not confirm or reject the token."""


def verify(token: str, secret: str, remote_ip: str = None) -> bool:
    data = {"secret": secret, "response": token}
    if remote_ip:
        data["remoteip"] = remote_ip
    try:
        response = requests.post(VERIFY_URL, data=data, timeout=TIMEOUT)
        response.raise_for_status()
        result = response.json()
    except (requests.RequestException, ValueError) as error:
        raise CaptchaUnavailable(str(error)) from error
    if result.get("success") is True:
        return True
    log.info("Turnstile rejected a token: %s", result.get("error-codes"))
    return False
