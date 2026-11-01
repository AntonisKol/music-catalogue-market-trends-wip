"""Write mapped Discogs records into PostgreSQL.

Every write is an upsert keyed on the Discogs id, so saving the same release
again updates it instead of adding a duplicate.
"""
import psycopg
from psycopg.types.json import Jsonb

from ingestion.mapping import map_release

UPSERT_LABEL = """
    INSERT INTO labels (id, name, resource_url, fetched_at)
    VALUES (%(id)s, %(name)s, %(resource_url)s, now())
    ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name,
        resource_url = EXCLUDED.resource_url,
        fetched_at = now()
"""

UPSERT_ARTIST = """
    INSERT INTO artists (id, name, resource_url, fetched_at)
    VALUES (%(id)s, %(name)s, %(resource_url)s, now())
    ON CONFLICT (id) DO UPDATE SET
        name = EXCLUDED.name,
        resource_url = EXCLUDED.resource_url,
        fetched_at = now()
"""

UPSERT_RELEASE = """
    INSERT INTO releases (id, master_id, title, year, released, country, label_id,
                          catno, formats, genres, styles, is_physical, fetched_at)
    VALUES (%(id)s, %(master_id)s, %(title)s, %(year)s, %(released)s, %(country)s,
            %(label_id)s, %(catno)s, %(formats)s, %(genres)s, %(styles)s,
            %(is_physical)s, now())
    ON CONFLICT (id) DO UPDATE SET
        master_id = EXCLUDED.master_id,
        title = EXCLUDED.title,
        year = EXCLUDED.year,
        released = EXCLUDED.released,
        country = EXCLUDED.country,
        label_id = EXCLUDED.label_id,
        catno = EXCLUDED.catno,
        formats = EXCLUDED.formats,
        genres = EXCLUDED.genres,
        styles = EXCLUDED.styles,
        is_physical = EXCLUDED.is_physical,
        fetched_at = now()
"""

# Artists credited on an earlier save but no longer on the release are removed.
DELETE_STALE_RELEASE_ARTISTS = """
    DELETE FROM release_artists
    WHERE release_id = %(release_id)s AND NOT (artist_id = ANY(%(artist_ids)s))
"""

INSERT_RELEASE_ARTIST = """
    INSERT INTO release_artists (release_id, artist_id)
    VALUES (%(release_id)s, %(artist_id)s)
    ON CONFLICT DO NOTHING
"""


def save_release(conn: psycopg.Connection, discogs_release: dict) -> int:
    """Save one Discogs release with its label and artists in a single transaction.

    Parents are written before the rows that reference them (label and artists,
    then the release, then the links). If any write fails, nothing is kept.
    Returns the release id.
    """
    mapped = map_release(discogs_release)
    release = dict(mapped["release"], formats=Jsonb(mapped["release"]["formats"]))
    release_id = release["id"]

    with conn.transaction(), conn.cursor() as cur:
        if mapped["label"]:
            cur.execute(UPSERT_LABEL, mapped["label"])
        for artist in mapped["artists"]:
            cur.execute(UPSERT_ARTIST, artist)
        cur.execute(UPSERT_RELEASE, release)
        cur.execute(
            DELETE_STALE_RELEASE_ARTISTS,
            {"release_id": release_id, "artist_ids": mapped["artist_ids"]},
        )
        for artist_id in mapped["artist_ids"]:
            cur.execute(
                INSERT_RELEASE_ARTIST, {"release_id": release_id, "artist_id": artist_id}
            )
    return release_id


def get_saved_release(conn: psycopg.Connection, release_id: int):
    """The stored release with its label and artist names, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT r.id, r.title, r.year, l.name,
                   COALESCE(array_agg(a.name ORDER BY a.name)
                            FILTER (WHERE a.id IS NOT NULL), '{}')
            FROM releases r
            LEFT JOIN labels l ON l.id = r.label_id
            LEFT JOIN release_artists ra ON ra.release_id = r.id
            LEFT JOIN artists a ON a.id = ra.artist_id
            WHERE r.id = %s
            GROUP BY r.id, l.name
            """,
            (release_id,),
        )
        return cur.fetchone()
