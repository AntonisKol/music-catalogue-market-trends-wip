"""save_release against a real, throwaway PostgreSQL (see conftest.py)."""
from datetime import date

import psycopg
import pytest

from ingestion.mapping import MappingError
from ingestion.repository import get_saved_release, save_release


def discogs_release(**overrides):
    release = {
        "id": 249504,
        "master_id": 96559,
        "title": "Never Gonna Give You Up",
        "year": 1987,
        "released": "1987-07-27",
        "country": "UK",
        "labels": [{"id": 895, "name": "RCA", "catno": "PB 41447", "resource_url": "https://api.discogs.com/labels/895"}],
        "artists": [{"id": 72872, "name": "Rick Astley", "resource_url": "https://api.discogs.com/artists/72872"}],
        "formats": [{"name": "Vinyl", "qty": "1", "descriptions": ['7"', "45 RPM"]}],
        "genres": ["Electronic", "Pop"],
        "styles": ["Synth-pop"],
    }
    release.update(overrides)
    return release


@pytest.fixture
def committed(postgres_url):
    """Run a query on a separate connection, so it only sees committed rows."""

    def query(sql, params=None):
        with psycopg.connect(postgres_url) as conn:
            return conn.execute(sql, params).fetchall()

    return query


def counts(committed):
    return {
        table: committed(f"SELECT count(*) FROM {table}")[0][0]
        for table in ("labels", "artists", "releases", "release_artists")
    }


def test_saves_release_with_label_and_artists(db, committed):
    assert save_release(db, discogs_release()) == 249504

    assert committed(
        "SELECT id, master_id, title, year, released, country, label_id, catno,"
        " formats, genres, styles, is_physical FROM releases"
    ) == [(
        249504, 96559, "Never Gonna Give You Up", 1987, date(1987, 7, 27), "UK", 895,
        "PB 41447", [{"name": "Vinyl", "qty": "1", "descriptions": ['7"', "45 RPM"]}],
        ["Electronic", "Pop"], ["Synth-pop"], True,
    )]
    assert committed("SELECT id, name, resource_url FROM labels") == [
        (895, "RCA", "https://api.discogs.com/labels/895")
    ]
    assert committed("SELECT id, name FROM artists") == [(72872, "Rick Astley")]
    assert committed("SELECT release_id, artist_id FROM release_artists") == [(249504, 72872)]


def test_get_saved_release(db):
    save_release(db, discogs_release())

    assert get_saved_release(db, 249504) == (
        249504, "Never Gonna Give You Up", 1987, "RCA", ["Rick Astley"]
    )
    assert get_saved_release(db, 1) is None


def test_saving_again_updates_instead_of_duplicating(db, committed):
    save_release(db, discogs_release())
    first_fetch = committed("SELECT fetched_at FROM releases")[0][0]

    save_release(db, discogs_release(
        title="Never Gonna Give You Up (Remastered)",
        labels=[{"id": 895, "name": "RCA Records", "catno": "PB 41447"}],
        artists=[{"id": 72872, "name": "Rick Astley (2)"}],
    ))

    assert counts(committed) == {"labels": 1, "artists": 1, "releases": 1, "release_artists": 1}
    title, fetched_at = committed("SELECT title, fetched_at FROM releases")[0]
    assert title == "Never Gonna Give You Up (Remastered)"
    assert fetched_at > first_fetch
    assert committed("SELECT name, resource_url FROM labels") == [("RCA Records", None)]
    assert committed("SELECT name FROM artists") == [("Rick Astley (2)",)]


def test_artists_no_longer_credited_are_unlinked(db, committed):
    save_release(db, discogs_release(artists=[{"id": 1, "name": "A"}, {"id": 2, "name": "B"}]))
    save_release(db, discogs_release(artists=[{"id": 2, "name": "B"}, {"id": 3, "name": "C"}]))

    assert committed("SELECT artist_id FROM release_artists ORDER BY artist_id") == [(2,), (3,)]
    # The artist row itself stays: other releases may credit it.
    assert committed("SELECT id FROM artists ORDER BY id") == [(1,), (2,), (3,)]


def test_resaving_one_release_keeps_other_releases_links(db, committed):
    save_release(db, discogs_release(id=1, artists=[{"id": 10, "name": "Shared"}]))
    save_release(db, discogs_release(id=2, artists=[{"id": 10, "name": "Shared"}]))
    save_release(db, discogs_release(id=1, artists=[{"id": 11, "name": "Other"}]))

    assert committed("SELECT release_id, artist_id FROM release_artists ORDER BY 1") == [
        (1, 11), (2, 10)
    ]


def test_release_with_no_label_or_artists(db, committed):
    save_release(db, discogs_release(labels=[], artists=[]))

    assert committed("SELECT label_id, catno FROM releases") == [(None, None)]
    assert counts(committed) == {"labels": 0, "artists": 0, "releases": 1, "release_artists": 0}


def test_failed_save_keeps_nothing(db, committed):
    # Label and artists are written first; the release row then fails (year out of INT range).
    with pytest.raises(psycopg.errors.NumericValueOutOfRange):
        save_release(db, discogs_release(year=10**10))

    assert counts(committed) == {"labels": 0, "artists": 0, "releases": 0, "release_artists": 0}


def test_failed_resave_keeps_the_earlier_version(db, committed):
    save_release(db, discogs_release())

    with pytest.raises(psycopg.Error):
        save_release(db, discogs_release(
            title="Changed",
            labels=[{"id": 895, "name": "Changed label"}],
            artists=[{"id": 99, "name": "New artist"}],
            year=10**10,
        ))

    assert committed("SELECT title FROM releases") == [("Never Gonna Give You Up",)]
    assert committed("SELECT name FROM labels") == [("RCA",)]
    assert committed("SELECT artist_id FROM release_artists") == [(72872,)]


def test_connection_still_works_after_a_failed_save(db, committed):
    with pytest.raises(psycopg.Error):
        save_release(db, discogs_release(id=1, year=10**10))

    save_release(db, discogs_release(id=2))

    assert committed("SELECT id FROM releases") == [(2,)]


def test_unmappable_release_writes_nothing(db, committed):
    with pytest.raises(MappingError):
        save_release(db, {"id": 5})

    assert counts(committed)["releases"] == 0
