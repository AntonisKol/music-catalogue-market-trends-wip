import requests
from ingestion import config
import time

BASE_URL = "https://api.discogs.com"

session = requests.Session()
session.headers.update({
    "Authorization": f"Discogs token={config.DISCOGS_TOKEN}",
    "User-Agent": config.DISCOGS_USER_AGENT,
})


def get_release(release_id: int) -> dict:
    response = session.get(f"{BASE_URL}/releases/{release_id}")
    response.raise_for_status()
    return response.json()


...

def get_label_releases(label_id: int) -> list[dict]:
    releases = []
    page = 1
    while True:
        response = session.get(
            f"{BASE_URL}/labels/{label_id}/releases",
            params={"page": page, "per_page": 100},
        )
        response.raise_for_status()
        data = response.json()
        releases.extend(data["releases"])

        if page >= data["pagination"]["pages"]:
            break
        page += 1
        time.sleep(1.1)

    return releases
