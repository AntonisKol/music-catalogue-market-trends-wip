"""Turn Discogs responses into rows for the catalogue and snapshot tables.

Pure functions only: no HTTP and no database, so they can be tested on their own.
"""
from datetime import date
from decimal import Decimal
from typing import Optional

# Discogs format names that are not a physical object.
DIGITAL_FORMATS = {"File"}


class MappingError(ValueError):
    """A Discogs response is missing data the catalogue needs."""


def parse_released(value: Optional[str]) -> Optional[date]:
    """Discogs gives "1999-03-14", "1999-03-00", "1999" or "". Only a full date is kept."""
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        return None


def is_physical(formats: list) -> bool:
    return any(f.get("name") not in DIGITAL_FORMATS for f in formats)


def map_label(release: dict) -> Optional[dict]:
    """The first label on the release, which the schema stores as releases.label_id."""
    for label in release.get("labels") or []:
        if label.get("id"):
            return {
                "id": label["id"],
                "name": label["name"],
                "resource_url": label.get("resource_url"),
                "catno": label.get("catno"),
            }
    return None


def map_artists(release: dict) -> list:
    """Unique credited artists, in order. Artists without a Discogs id are skipped."""
    artists = {}
    for artist in release.get("artists") or []:
        if artist.get("id") and artist["id"] not in artists:
            artists[artist["id"]] = {
                "id": artist["id"],
                "name": artist["name"],
                "resource_url": artist.get("resource_url"),
            }
    return list(artists.values())


def map_release(release: dict) -> dict:
    """Everything needed to save one release: its label, artists and the release row."""
    if not release.get("id") or not release.get("title"):
        raise MappingError("Discogs release has no id or title")

    label = map_label(release)
    artists = map_artists(release)
    formats = release.get("formats") or []
    return {
        "label": label,
        "artists": artists,
        "release": {
            "id": release["id"],
            "master_id": release.get("master_id") or None,
            "title": release["title"],
            "year": release.get("year") or None,
            "released": parse_released(release.get("released")),
            "country": release.get("country") or None,
            "label_id": label["id"] if label else None,
            "catno": label["catno"] if label else None,
            "formats": formats,
            "genres": release.get("genres") or [],
            "styles": release.get("styles") or [],
            "is_physical": is_physical(formats),
        },
        "artist_ids": [a["id"] for a in artists],
    }


def map_price_snapshot(release_id: int, stats: dict, snapshot_date: date) -> dict:
    """A price_snapshots row from /marketplace/stats.

    With nothing for sale Discogs sends lowest_price null: price and currency stay
    NULL, while num_for_sale keeps its real count (0).
    """
    lowest = stats.get("lowest_price") or {}
    value = lowest.get("value")
    return {
        "release_id": release_id,
        "snapshot_date": snapshot_date,
        "num_for_sale": stats.get("num_for_sale"),
        # Via str, so 0.59 is stored as 0.59 and not as the nearest binary float.
        "lowest_price": Decimal(str(value)) if value is not None else None,
        "currency": lowest.get("currency") if value is not None else None,
    }


def map_demand_snapshot(release_id: int, release: dict, snapshot_date: date) -> dict:
    """A demand_snapshots row from the community block of /releases/{id}.

    Discogs gives an average of 0.0 when nobody has rated the release; that is
    stored as NULL (not rated), not as the lowest possible rating.
    """
    community = release.get("community") or {}
    rating = community.get("rating") or {}
    rating_count = rating.get("count")
    average = rating.get("average")
    return {
        "release_id": release_id,
        "snapshot_date": snapshot_date,
        "want": community.get("want"),
        "have": community.get("have"),
        "rating_count": rating_count,
        "rating_average": Decimal(str(average)) if rating_count and average is not None else None,
    }
