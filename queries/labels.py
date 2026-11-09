"""The labels the site currently follows, for the list on the question box."""
import re
from collections import Counter

from queries.database import connect_reader

# Discogs tells labels with the same name apart by a number: "Afterlife (6)".
DISCOGS_NUMBER = re.compile(r"\s*\(\d+\)$")


def _names(where: str) -> list:
    with connect_reader() as conn:
        rows = conn.execute("SELECT t.name FROM tracked_labels t WHERE t.active" + where).fetchall()
    full = {name for (name,) in rows}
    shared = Counter(DISCOGS_NUMBER.sub("", name) for name in full)
    # The number is dropped only when no other listed label has the same name, so two
    # labels called "Wax" stay apart as "Wax (4)" and "Wax (9)".
    names = {DISCOGS_NUMBER.sub("", name) if shared[DISCOGS_NUMBER.sub("", name)] == 1 else name
             for name in full}
    return sorted(names, key=lambda name: (name.lower(), name))


def tracked_label_names() -> list:
    """Names of the active tracked labels, as a visitor would know them, in alphabetical
    order, including ones still being imported. Paused labels are left out."""
    return _names("")


def listed_label_names() -> list:
    """The tracked labels the page lists: only those whose releases are in the database,
    so a label appears once questions about it can be answered."""
    return _names(" AND EXISTS (SELECT 1 FROM releases r WHERE r.label_id = t.label_id)")


def data_dates() -> dict:
    """The first and the latest day prices were saved, for "About the data" on the page,
    as ISO dates, or None before the first night. The index on snapshot_date keeps this
    to two index lookups however long the history grows."""
    with connect_reader() as conn:
        first, latest = conn.execute("SELECT min(snapshot_date), max(snapshot_date) FROM price_snapshots").fetchone()
    return {"tracked_since": first and first.isoformat(), "updated": latest and latest.isoformat()}
