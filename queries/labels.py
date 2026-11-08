"""The labels the site currently follows, for the list on the question box."""
import re

from queries.database import connect_reader

# Discogs tells labels with the same name apart by a number: "Afterlife (6)".
DISCOGS_NUMBER = re.compile(r"\s*\(\d+\)$")


def _names(where: str) -> list:
    with connect_reader() as conn:
        rows = conn.execute("SELECT t.name FROM tracked_labels t WHERE t.active" + where).fetchall()
    names = {DISCOGS_NUMBER.sub("", name) for (name,) in rows}
    return sorted(names, key=lambda name: (name.lower(), name))


def tracked_label_names() -> list:
    """Names of the active tracked labels, as a visitor would know them, in alphabetical
    order, including ones still being imported. Paused labels are left out."""
    return _names("")


def listed_label_names() -> list:
    """The tracked labels the page lists: only those whose releases are in the database,
    so a label appears once questions about it can be answered."""
    return _names(" AND EXISTS (SELECT 1 FROM releases r WHERE r.label_id = t.label_id)")
