"""The labels the site currently follows, for the list on the question box."""
import re

from queries.database import connect_reader

# Discogs tells labels with the same name apart by a number: "Afterlife (6)".
DISCOGS_NUMBER = re.compile(r"\s*\(\d+\)$")


def tracked_label_names() -> list:
    """Names of the active tracked labels, as a visitor would know them, in alphabetical order.
    Paused labels are left out."""
    with connect_reader() as conn:
        rows = conn.execute("SELECT name FROM tracked_labels WHERE active").fetchall()
    names = {DISCOGS_NUMBER.sub("", name) for (name,) in rows}
    return sorted(names, key=lambda name: (name.lower(), name))
