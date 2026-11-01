"""Email alerts for the nightly run, sent over SMTP.

Optional settings in .env; without ALERT_EMAIL_TO and SMTP_HOST nothing is sent,
and the alert is only logged:

    ALERT_EMAIL_TO=you@example.com
    ALERT_EMAIL_FROM=alerts@example.com    (defaults to SMTP_USER)
    SMTP_HOST=smtp.example.com
    SMTP_PORT=587                           (STARTTLS)
    SMTP_USER=...
    SMTP_PASSWORD=...

Any provider with SMTP works, including the one chosen for sign-in emails.
"""
import logging
import os
import smtplib
from email.message import EmailMessage

log = logging.getLogger(__name__)
TIMEOUT = 30


def send_alert(subject: str, body: str) -> bool:
    """Send one alert email. Returns True if it was sent; never raises."""
    to = os.environ.get("ALERT_EMAIL_TO")
    host = os.environ.get("SMTP_HOST")
    if not to or not host:
        log.warning("Alert not emailed (ALERT_EMAIL_TO / SMTP_HOST not set): %s", subject)
        return False
    user = os.environ.get("SMTP_USER")
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = os.environ.get("ALERT_EMAIL_FROM") or user or to
    message["To"] = to
    message.set_content(body)
    try:
        with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT", "587")), timeout=TIMEOUT) as smtp:
            smtp.starttls()
            if user:
                smtp.login(user, os.environ.get("SMTP_PASSWORD", ""))
            smtp.send_message(message)
    except (OSError, smtplib.SMTPException) as error:
        log.error("Alert email could not be sent: %s", error)
        return False
    log.info("Alert emailed to %s: %s", to, subject)
    return True
