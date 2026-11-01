"""Write mapped Discogs records into PostgreSQL.

Every write is an upsert, so saving the same release again updates it instead of
adding a duplicate, and a second snapshot on the same day replaces that day's.
"""
from datetime import date

import psycopg
from psycopg.types.json import Jsonb

from ingestion.mapping import map_demand_snapshot, map_price_snapshot, map_release

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


UPSERT_PRICE_SNAPSHOT = """
    INSERT INTO price_snapshots (release_id, snapshot_date, num_for_sale, lowest_price, currency)
    VALUES (%(release_id)s, %(snapshot_date)s, %(num_for_sale)s, %(lowest_price)s, %(currency)s)
    ON CONFLICT (release_id, snapshot_date) DO UPDATE SET
        num_for_sale = EXCLUDED.num_for_sale,
        lowest_price = EXCLUDED.lowest_price,
        currency = EXCLUDED.currency
"""

UPSERT_DEMAND_SNAPSHOT = """
    INSERT INTO demand_snapshots (release_id, snapshot_date, want, have, rating_count, rating_average)
    VALUES (%(release_id)s, %(snapshot_date)s, %(want)s, %(have)s, %(rating_count)s, %(rating_average)s)
    ON CONFLICT (release_id, snapshot_date) DO UPDATE SET
        want = EXCLUDED.want,
        have = EXCLUDED.have,
        rating_count = EXCLUDED.rating_count,
        rating_average = EXCLUDED.rating_average
"""


def save_snapshots(
    conn: psycopg.Connection,
    release_id: int,
    marketplace_stats: dict,
    discogs_release: dict,
    snapshot_date: date,
) -> None:
    """Save one day's price and demand snapshot of a release in a single transaction.

    The release must already be saved (the snapshots reference it). A second save on
    the same snapshot_date replaces that day's values. If either write fails, neither
    is kept; the catalogue rows are untouched.
    """
    price = map_price_snapshot(release_id, marketplace_stats, snapshot_date)
    demand = map_demand_snapshot(release_id, discogs_release, snapshot_date)
    with conn.transaction(), conn.cursor() as cur:
        cur.execute(UPSERT_PRICE_SNAPSHOT, price)
        cur.execute(UPSERT_DEMAND_SNAPSHOT, demand)


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


def get_saved_snapshot(conn: psycopg.Connection, release_id: int, snapshot_date: date):
    """The stored price and demand snapshot of a release on a day, or None."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT p.num_for_sale, p.lowest_price, p.currency,
                   d.want, d.have, d.rating_count, d.rating_average
            FROM price_snapshots p
            JOIN demand_snapshots d USING (release_id, snapshot_date)
            WHERE release_id = %s AND snapshot_date = %s
            """,
            (release_id, snapshot_date),
        )
        return cur.fetchone()
