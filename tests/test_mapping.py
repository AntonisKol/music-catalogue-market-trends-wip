from datetime import date
from decimal import Decimal

import pytest

from ingestion.mapping import (
    MappingError,
    map_demand_snapshot,
    map_price_snapshot,
    map_release,
    parse_released,
)


def discogs_release(**overrides):
    release = {
        "id": 249504,
        "master_id": 96559,
        "title": "Never Gonna Give You Up",
        "year": 1987,
        "released": "1987-07-27",
        "country": "UK",
        "labels": [
            {"id": 895, "name": "RCA", "catno": "PB 41447", "resource_url": "https://api.discogs.com/labels/895"},
            {"id": 1, "name": "Second Label", "catno": "X1"},
        ],
        "artists": [
            {"id": 72872, "name": "Rick Astley", "resource_url": "https://api.discogs.com/artists/72872"},
        ],
        "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ['7"', "Single", "45 RPM"]}],
        "genres": ["Electronic", "Pop"],
        "styles": ["Synth-pop"],
    }
    release.update(overrides)
    return release


def test_maps_release_row():
    row = map_release(discogs_release())["release"]
    assert row == {
        "id": 249504,
        "master_id": 96559,
        "title": "Never Gonna Give You Up",
        "year": 1987,
        "released": date(1987, 7, 27),
        "country": "UK",
        "label_id": 895,
        "catno": "PB 41447",
        "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ['7"', "Single", "45 RPM"]}],
        "genres": ["Electronic", "Pop"],
        "styles": ["Synth-pop"],
        "is_physical": True,
    }


def test_uses_first_label():
    assert map_release(discogs_release())["label"] == {
        "id": 895,
        "name": "RCA",
        "resource_url": "https://api.discogs.com/labels/895",
        "catno": "PB 41447",
    }


def test_keeps_every_label_once_in_order():
    mapped = map_release(discogs_release(labels=[
        {"id": 7, "name": "Seven", "catno": "S1"},
        {"name": "No id", "catno": "X"},
        {"id": 8, "name": "Eight", "catno": "E1"},
        {"id": 7, "name": "Seven", "catno": "S2"},
    ]))
    assert [(label["id"], label["catno"]) for label in mapped["labels"]] == [(7, "S1"), (8, "E1")]
    assert (mapped["release"]["label_id"], mapped["release"]["catno"]) == (7, "S1")


def test_release_without_label():
    mapped = map_release(discogs_release(labels=[]))
    assert mapped["label"] is None
    assert mapped["labels"] == []
    assert mapped["release"]["label_id"] is None
    assert mapped["release"]["catno"] is None


def test_artists_are_unique_and_need_an_id():
    artists = [
        {"id": 1, "name": "A"},
        {"id": 2, "name": "B"},
        {"id": 1, "name": "A"},
        {"id": 0, "name": "No id"},
        {"name": "Also no id"},
    ]
    mapped = map_release(discogs_release(artists=artists))
    assert [a["name"] for a in mapped["artists"]] == ["A", "B"]
    assert mapped["artist_ids"] == [1, 2]
    assert mapped["artists"][0]["resource_url"] is None


@pytest.mark.parametrize(
    "value, expected",
    [("1999-03-14", date(1999, 3, 14)), ("1999-03-00", None), ("1999", None), ("", None), (None, None)],
)
def test_parse_released_keeps_only_full_dates(value, expected):
    assert parse_released(value) == expected


@pytest.mark.parametrize(
    "formats, expected",
    [
        ([{"name": "Vinyl"}], True),
        ([{"name": "File"}], False),
        ([{"name": "File"}, {"name": "CD"}], True),
        ([], False),
    ],
)
def test_is_physical(formats, expected):
    assert map_release(discogs_release(formats=formats))["release"]["is_physical"] is expected


def test_missing_optional_fields_become_none_or_empty():
    release = {"id": 5, "title": "Bare", "year": 0, "country": ""}
    row = map_release(release)["release"]
    assert row["year"] is None
    assert row["country"] is None
    assert row["master_id"] is None
    assert row["genres"] == [] and row["styles"] == [] and row["formats"] == []
    assert map_release(release)["artists"] == []


@pytest.mark.parametrize("release", [{"title": "No id"}, {"id": 5}, {"id": 5, "title": ""}])
def test_rejects_release_without_id_or_title(release):
    with pytest.raises(MappingError):
        map_release(release)


DAY = date(2026, 11, 1)


def test_maps_price_snapshot():
    stats = {"num_for_sale": 124, "lowest_price": {"value": 0.59, "currency": "EUR"}, "blocked_from_sale": False}
    assert map_price_snapshot(249504, stats, DAY) == {
        "release_id": 249504,
        "snapshot_date": DAY,
        "num_for_sale": 124,
        "lowest_price": Decimal("0.59"),
        "currency": "EUR",
    }


@pytest.mark.parametrize(
    "lowest_price",
    [None, {"value": None, "currency": "EUR"}],
    ids=["live answer: price null", "currency without a value"],
)
def test_nothing_for_sale_keeps_the_count_but_has_no_price(lowest_price):
    stats = {"num_for_sale": 0, "lowest_price": lowest_price, "blocked_from_sale": False}
    row = map_price_snapshot(1, stats, DAY)
    assert row["num_for_sale"] == 0
    assert row["lowest_price"] is None and row["currency"] is None


def test_maps_demand_snapshot():
    release = {"community": {"have": 4159, "want": 594, "rating": {"count": 249, "average": 3.88}}}
    assert map_demand_snapshot(249504, release, DAY) == {
        "release_id": 249504,
        "snapshot_date": DAY,
        "want": 594,
        "have": 4159,
        "rating_count": 249,
        "rating_average": Decimal("3.88"),
    }


def test_no_ratings_is_not_rated_rather_than_zero():
    # Discogs sends average 0.0 when nobody has rated the release.
    release = {"community": {"have": 1, "want": 0, "rating": {"count": 0, "average": 0.0}}}
    row = map_demand_snapshot(1, release, DAY)
    assert (row["want"], row["have"], row["rating_count"]) == (0, 1, 0)
    assert row["rating_average"] is None


def test_missing_community_block_gives_unknown_values():
    row = map_demand_snapshot(1, {"id": 1, "title": "Bare"}, DAY)
    assert [row[k] for k in ("want", "have", "rating_count", "rating_average")] == [None] * 4
