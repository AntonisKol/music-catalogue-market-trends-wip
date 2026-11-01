from datetime import date

import pytest

from ingestion.mapping import MappingError, map_release, parse_released


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


def test_release_without_label():
    mapped = map_release(discogs_release(labels=[]))
    assert mapped["label"] is None
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
