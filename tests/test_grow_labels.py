"""Adding the most-collected techno labels each night, while the nightly run has room (issue 81).

Discogs is replaced by a small fake at the client's functions; the database is real.
The last run measured 36 seconds per tracked release (1 hour for 100), so the
20-hour budget holds 2,000 releases: 1,900 more than the 100 tracked.
"""
from datetime import datetime, timedelta, timezone

import pytest
import responses

from ingestion import discogs_client, grow_labels, track_labels

NOW = datetime(2026, 11, 9, 3, 0, tzinfo=timezone.utc)


class FakeDiscogs:
    """Labels: name -> (id, vinyl, techno, exact size). Ranking: one first-label name per release, most had first."""

    def __init__(self, monkeypatch):
        self.labels = {}
        self.ranking = []
        self.calls = []
        monkeypatch.setattr(discogs_client, "search_releases", self.search_releases)
        monkeypatch.setattr(discogs_client, "search_labels", self.search_labels)
        monkeypatch.setattr(discogs_client, "label_size", self.label_size)
        monkeypatch.setattr(track_labels, "get_label", lambda label_id: {"name": self.name_of(label_id)})

    def name_of(self, label_id):
        return next(name for name, (lid, *_rest) in self.labels.items() if lid == label_id)

    def search_releases(self, page=1, per_page=100, **filters):
        self.calls.append(("search", filters.get("label"), page))
        if "label" in filters:
            _id, vinyl, techno, _size = self.labels[filters["label"]]
            return {"pagination": {"items": techno if filters.get("style") == "Techno" else vinyl, "pages": 1},
                    "results": []}
        start = (page - 1) * per_page
        results = [{"label": [name, "Some Distributor"]} for name in self.ranking[start:start + per_page]]
        return {"pagination": {"items": len(self.ranking), "pages": max(1, -(-len(self.ranking) // per_page))},
                "results": results}

    def search_labels(self, name, limit=5):
        self.calls.append(("label", name))
        return [{"id": lid, "title": title} for title, (lid, *_rest) in self.labels.items()
                if name.casefold() in title.casefold()][:limit]

    def label_size(self, label_id):
        return next(size for lid, _v, _t, size in self.labels.values() if lid == label_id)

    def looked_up(self):
        return [call[1] for call in self.calls if call[0] == "label"]


@pytest.fixture
def discogs(monkeypatch):
    return FakeDiscogs(monkeypatch)


@pytest.fixture
def tracked(db, limits_role):
    """Tresor is tracked with 100 releases, and the last run took an hour."""
    db.execute("INSERT INTO labels (id, name) VALUES (1, 'Tresor')")
    db.execute("INSERT INTO tracked_labels (label_id, name) VALUES (1, 'Tresor')")
    for release_id in range(1, 101):
        db.execute("INSERT INTO releases (id, title, label_id, is_physical) VALUES (%s, 'x', 1, true)", (release_id,))
    db.execute("INSERT INTO ingestion_runs (snapshot_date, started_at, finished_at, status)"
               " VALUES ('2026-11-08', %s, %s, 'succeeded')", (NOW - timedelta(hours=2), NOW - timedelta(hours=1)))
    db.commit()
    yield db
    db.rollback()
    db.execute("TRUNCATE label_requests, accounts RESTART IDENTITY CASCADE")
    db.commit()


def added(db):
    db.rollback()
    return db.execute("SELECT name FROM tracked_labels WHERE added_by = 'nightly' ORDER BY added_at, name").fetchall()


def verdicts(db):
    db.rollback()
    return dict(db.execute("SELECT name, verdict FROM label_candidates").fetchall())


def test_the_most_collected_techno_labels_come_first_ten_a_night(tracked, discogs):
    names = [f"Label {n:02}" for n in range(1, 13)]
    for number, name in enumerate(names, start=10):
        discogs.labels[name] = (number, 50, 40, 60)
    # Label 12 released the most of the most-had releases, then 11, and so on.
    discogs.ranking = [name for count, name in enumerate(names, start=1) for _ in range(count)][::-1]

    result = grow_labels.grow(tracked, NOW)

    assert result == [f"Label {n:02}" for n in range(12, 2, -1)]
    assert len(added(tracked)) == 10
    assert verdicts(tracked) == {name: "added" for name in result}


def test_labels_that_arent_mostly_techno_or_are_too_big_are_left_out(tracked, discogs):
    discogs.labels = {
        "Virgin": (20, 1500, 30, 60000),                # 2% techno
        "Huge Techno": (21, 2500, 2400, 3000),          # too many releases
        "Planet Rhythm": (22, 300, 250, 400),
        "R & S Records": (23, 900, 400, 2000),          # 44%: enough
    }
    discogs.ranking = ["Virgin", "Huge Techno", "Planet Rhythm", "R & S Records", "Nobody Knows"]

    assert grow_labels.grow(tracked, NOW) == ["Planet Rhythm", "R & S Records"]
    assert verdicts(tracked) == {"Virgin": "not_techno", "Huge Techno": "too_big", "Planet Rhythm": "added",
                                 "R & S Records": "added", "Nobody Knows": "not_found"}


def test_a_similar_name_does_not_inflate_a_labels_count(tracked, discogs):
    # Discogs' name filter also counts other "M-Plant ..." labels; the exact size is 200.
    discogs.labels["M-Plant"] = (24, 12112, 162, 200)
    discogs.ranking = ["M-Plant"]

    assert grow_labels.grow(tracked, NOW) == ["M-Plant"]
    assert tracked.execute("SELECT vinyl, techno FROM label_candidates").fetchone() == (200, 162)


def test_tracked_paused_and_rejected_labels_are_never_added(tracked, discogs):
    tracked.execute("INSERT INTO tracked_labels (label_id, name, active) VALUES (30, 'Paused One', false)")
    tracked.execute("INSERT INTO accounts (email, password_hash, created_at) VALUES ('a@example.com', 'x', now())")
    tracked.execute("INSERT INTO label_requests (account_id, name, status, created_at) VALUES"
                    " (1, 'Polegroup', 'rejected', now())")
    tracked.commit()
    # "Tresor Berlin" is Discogs' name in a release; it is the tracked Tresor (id 1).
    discogs.labels = {"Tresor Berlin": (1, 700, 650, 734), "Paused One": (30, 100, 90, 100),
                      "Polegroup Records": (31, 100, 90, 100)}
    discogs.ranking = ["Tresor", "Tresor Records", "Tresor Berlin", "Paused One", "Polegroup Records"]

    assert grow_labels.grow(tracked, NOW) == []
    assert verdicts(tracked) == {"Tresor Berlin": "tracked", "Polegroup Records": "rejected"}
    assert discogs.looked_up() == ["Tresor Berlin"]


def test_nothing_is_added_past_twenty_hours_even_if_a_smaller_label_would_fit(tracked, discogs):
    discogs.labels = {"First": (40, 1200, 1000, 1300), "Second": (41, 800, 700, 900), "Small": (42, 10, 9, 10)}
    discogs.ranking = ["First", "Second", "Small"]

    assert grow_labels.grow(tracked, NOW) == ["First"]
    assert verdicts(tracked) == {"First": "added", "Second": "no_room"}


def test_at_the_ceiling_discogs_is_not_even_asked(tracked, discogs):
    tracked.execute("UPDATE ingestion_runs SET started_at = finished_at - interval '20 hours'")
    tracked.commit()
    discogs.ranking = ["Anything"]

    assert grow_labels.grow(tracked, NOW) == []
    assert discogs.calls == []


def test_a_skipped_night_does_not_count_as_the_last_runs_speed(tracked, discogs):
    """A skipped run (issue 98) took no time; the speed still comes from the hour-long run."""
    tracked.execute("UPDATE ingestion_runs SET started_at = finished_at - interval '20 hours'")
    tracked.execute("INSERT INTO ingestion_runs (snapshot_date, started_at, finished_at, status, message)"
                    " VALUES ('2026-11-09', %s, %s, 'skipped', 'Not started')", (NOW, NOW))
    tracked.commit()
    discogs.ranking = ["Anything"]

    assert grow_labels.grow(tracked, NOW) == []
    assert discogs.calls == []


def test_without_a_finished_run_nothing_is_added(db, limits_role, discogs):
    discogs.ranking = ["Anything"]

    assert grow_labels.grow(db, NOW) == []
    assert discogs.calls == []


def test_labels_added_but_not_imported_yet_count_towards_the_next_night(tracked, discogs):
    discogs.labels = {"First": (40, 1200, 1000, 1300), "Second": (41, 800, 700, 900)}
    discogs.ranking = ["First"]
    grow_labels.grow(tracked, NOW)

    # The import of First failed overnight: its 1,200 releases still count.
    discogs.ranking = ["Second"]
    assert grow_labels.grow(tracked, NOW + timedelta(days=1)) == []
    assert verdicts(tracked)["Second"] == "no_room"


def test_left_out_labels_are_looked_at_again_only_after_90_days_but_waiting_ones_every_night(tracked, discogs):
    discogs.labels = {"Virgin": (20, 1500, 30, 60000), "First": (40, 1200, 1000, 1300),
                      "Second": (41, 800, 700, 900)}
    discogs.ranking = ["Virgin", "First", "Second"]
    grow_labels.grow(tracked, NOW)
    discogs.calls.clear()

    grow_labels.grow(tracked, NOW + timedelta(days=1))
    assert discogs.looked_up() == ["Second"]

    discogs.calls.clear()
    grow_labels.grow(tracked, NOW + timedelta(days=91))
    assert discogs.looked_up() == ["Virgin", "Second"]


def test_the_owner_can_see_why_each_label_was_added_or_left_out(tracked, discogs, owner_db, capsys):
    discogs.labels = {"Virgin": (20, 1500, 30, 60000), "Planet Rhythm": (22, 300, 250, 400)}
    discogs.ranking = ["Virgin", "Planet Rhythm"]
    grow_labels.grow(tracked, NOW)

    assert grow_labels.main(["list"]) == 0

    out = capsys.readouterr().out
    assert "Planet Rhythm  (22)  250/300 techno vinyl: added" in out
    assert "Virgin  (20)  30/1500 techno vinyl: fewer than a third of its vinyl releases are techno" in out


@pytest.fixture
def owner_db(postgres_url, monkeypatch):
    import psycopg
    from ingestion import database
    monkeypatch.setattr(database, "connect", lambda: psycopg.connect(postgres_url))


def test_wrong_use_shows_how(capsys):
    assert grow_labels.main(["add"]) == 2
    assert "Usage" in capsys.readouterr().err


@responses.activate
def test_counting_asks_discogs_for_one_result_with_the_filters():
    responses.get(f"{discogs_client.BASE_URL}/database/search", json={"pagination": {"items": 686}, "results": []})

    assert discogs_client.count_releases(label="Tresor", format="Vinyl", style="Techno") == 686

    sent = responses.calls[0].request.params
    assert sent == {"type": "release", "page": "1", "per_page": "1", "label": "Tresor", "format": "Vinyl",
                    "style": "Techno"}
