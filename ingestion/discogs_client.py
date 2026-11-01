import logging
import time

import requests

from ingestion import config

BASE_URL = "https://api.discogs.com"

# Discogs allows 60 authenticated requests a minute. Keeping every request at
# least this far apart stays just under that (about 55 a minute).
MIN_INTERVAL = 0.0

# Waits before each retry of a temporary failure. After the last one the
# request has waited 80 s in total, longer than Discogs' 60 s window.
RETRY_WAITS = (5, 15, 60)

# Rate limited or a passing server problem: worth trying again. Anything else
# (401, 404, ...) fails the same way every time, so it is raised at once.
TEMPORARY_STATUSES = {429, 500, 502, 503, 504}

REQUEST_TIMEOUT = 30

log = logging.getLogger(__name__)

session = requests.Session()
session.headers.update({
    "Authorization": f"Discogs token={config.DISCOGS_TOKEN}",
    "User-Agent": config.DISCOGS_USER_AGENT,
})

# Indirections so tests can replace the clock without touching the time module.
_sleep = time.sleep
_now = time.monotonic
_last_request = None


def _wait_for_turn() -> None:
    global _last_request
    if _last_request is not None:
        remaining = MIN_INTERVAL - (_now() - _last_request)
        if remaining > 0:
            _sleep(remaining)
    _last_request = _now()


def _get(url: str, params: dict = None) -> dict:
    """GET a Discogs URL, paced, retrying only temporary failures."""
    for wait in (*RETRY_WAITS, None):
        _wait_for_turn()
        try:
            response = session.get(url, params=params, timeout=REQUEST_TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as error:
            if wait is None:
                raise
            log.warning("Discogs request failed (%s); retrying in %s s", error, wait)
            _sleep(wait)
            continue
        if response.status_code in TEMPORARY_STATUSES and wait is not None:
            log.warning("Discogs answered %s for %s; retrying in %s s", response.status_code, url, wait)
            _sleep(wait)
            continue
        response.raise_for_status()
        return response.json()


def get_release(release_id: int) -> dict:
    return _get(f"{BASE_URL}/releases/{release_id}")


def get_marketplace_stats(release_id: int, currency: str) -> dict:
    """Copies for sale and the lowest price, in the currency asked for.

    /releases/{id} also has a lowest_price, but always in the token owner's
    account currency and without saying which, so prices come from here.
    """
    return _get(f"{BASE_URL}/marketplace/stats/{release_id}", params={"curr_abbr": currency})


def get_label_releases(label_id: int) -> list[dict]:
    releases = []
    page = 1
    while True:
        data = _get(f"{BASE_URL}/labels/{label_id}/releases", params={"page": page, "per_page": 100})
        releases.extend(data["releases"])

        if page >= data["pagination"]["pages"]:
            break
        page += 1

    return releases
