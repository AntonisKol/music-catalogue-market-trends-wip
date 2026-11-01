"""The query tools, run through the reader login against a small dataset over three days."""
import json
from datetime import date

import pytest

from ingestion.repository import save_release, save_snapshots
from queries.tools import MAX_LIMIT, TOOLS, ToolError, run_tool

OCT_1, OCT_15, NOV_1 = date(2026, 10, 1), date(2026, 10, 15), date(2026, 11, 1)
VINYL = [{"name": "Vinyl", "descriptions": ['12"']}]


def release(release_id, title, artist, label, year, styles, country="US", genres=("Electronic",)):
    return {
        "id": release_id, "title": title, "year": year, "country": country,
        "labels": [{"id": label[0], "name": label[1], "catno": f"CAT{release_id}"}],
        "artists": [{"id": artist[0], "name": artist[1]}],
        "formats": VINYL, "genres": list(genres), "styles": list(styles),
    }


PLANET_E = (1, "Planet E")
PLANET_EARTH = (2, "Planet Earth Records")
UR = (3, "Underground Resistance")

RELEASES = {
    101: release(101, "Throw", (11, "Paperclip People"), PLANET_E, 1994, ["Techno"]),
    102: release(102, "Strings Of Life", (12, "Rhythim Is Rhythim"), PLANET_E, 1987, ["Deep House"]),
    103: release(103, "Sold Out", (13, "Carl Craig"), PLANET_E, 1996, ["Techno"]),
    104: release(104, "Unrated", (13, "Carl Craig"), PLANET_EARTH, 2001, ["Ambient"], country="UK"),
    105: release(105, "Riot 100% _ Mix", (14, "Mad Mike"), UR, 1992, ["Techno"]),
}

# release: [(day, num_for_sale, lowest_price, want, have, rating_count, rating_average)]
SNAPSHOTS = {
    101: [(OCT_1, 20, 10.0, 100, 500, 50, 4.5), (OCT_15, 15, 12.0, 120, 500, 50, 4.5), (NOV_1, 10, 15.0, 150, 500, 50, 4.5)],
    102: [(OCT_1, 5, 30.0, 300, 200, 120, 4.8), (OCT_15, 5, 28.0, 300, 200, 120, 4.8), (NOV_1, 6, 25.0, 300, 200, 120, 4.8)],
    103: [(OCT_1, 2, 40.0, 80, 50, 3, 4.0), (OCT_15, 1, 45.0, 90, 50, 3, 4.0), (NOV_1, 0, None, 95, 50, 3, 4.0)],
    104: [(NOV_1, 30, 5.0, 2, 10, 0, 0.0)],
    105: [(NOV_1, 3, 8.0, 40, 20, 10, 3.9)],
}


@pytest.fixture
def data(db):
    for release_id, discogs_release in RELEASES.items():
        save_release(db, discogs_release)
        for day, for_sale, price, want, have, count, average in SNAPSHOTS[release_id]:
            stats = {"num_for_sale": for_sale, "lowest_price": {"value": price, "currency": "EUR"} if price else None}
            community = {"want": want, "have": have, "rating": {"count": count, "average": average}}
            save_snapshots(db, release_id, stats, dict(discogs_release, community=community), day)


def ids(result):
    return [row["release_id"] for row in result["rows"]]


# --- search_releases -----------------------------------------------------------

def test_search_matches_part_of_a_name_in_any_case(data, reader):
    result = run_tool(reader, "search_releases", {"artist": "CRAIG"})

    assert ids(result) == [103, 104]
    assert result["rows"][0]["artists"] == ["Carl Craig"]


def test_search_returns_the_label_so_similar_names_can_be_told_apart(data, reader):
    result = run_tool(reader, "search_releases", {"label": "planet e"})

    assert {row["label"] for row in result["rows"]} == {"Planet E", "Planet Earth Records"}


def test_search_combines_filters(data, reader):
    result = run_tool(reader, "search_releases", {"style": "techno", "year_from": 1993, "year_to": 1999})

    assert ids(result) == [101, 103]


def test_search_includes_the_latest_snapshot(data, reader):
    row = run_tool(reader, "search_releases", {"title": "throw"})["rows"][0]

    assert row["as_of"] == "2026-11-01"
    assert (row["num_for_sale"], row["lowest_price"], row["currency"]) == (10, 15.0, "EUR")
    assert (row["want"], row["have"], row["rating_average"]) == (150, 500, 4.5)


def test_results_are_capped_and_say_how_many_matched(data, reader):
    result = run_tool(reader, "search_releases", {"limit": 2})

    assert result["returned"] == 2
    assert result["total"] == 5
    assert result["truncated"] is True


def test_no_match_is_an_empty_result(data, reader):
    assert run_tool(reader, "search_releases", {"title": "nothing like this"}) == {
        "rows": [], "returned": 0, "total": 0, "truncated": False
    }


# --- rank_releases -------------------------------------------------------------

def test_rank_by_lowest_price_skips_releases_with_nothing_for_sale(data, reader):
    assert ids(run_tool(reader, "rank_releases", {"sort_by": "lowest_price"})) == [104, 105, 101, 102]


def test_rank_best_rated_needs_enough_votes_and_skips_unrated(data, reader):
    assert ids(run_tool(reader, "rank_releases", {"sort_by": "best_rated"})) == [102, 101, 105]
    assert ids(run_tool(reader, "rank_releases", {"sort_by": "best_rated", "min_ratings": 1})) == [102, 101, 103, 105]


def test_rank_fewest_for_sale_puts_sold_out_first(data, reader):
    assert ids(run_tool(reader, "rank_releases", {"sort_by": "fewest_for_sale"}))[0] == 103


def test_rank_most_wanted_per_owner(data, reader):
    # want / have: 105 2.0, 103 1.9, 102 1.5, 101 0.3, 104 0.2
    assert ids(run_tool(reader, "rank_releases", {"sort_by": "most_wanted_per_owner"})) == [105, 103, 102, 101, 104]


def test_rank_with_filters(data, reader):
    result = run_tool(reader, "rank_releases", {"sort_by": "most_wanted", "label": "Planet E", "year_to": 1995})

    assert ids(result) == [102, 101]
    assert result["sort_by"] == "most_wanted"


# --- release_history -----------------------------------------------------------

def test_release_history_day_by_day(data, reader):
    result = run_tool(reader, "release_history", {"release_id": 101, "days": 90})

    assert result["release"]["title"] == "Throw"
    assert [(r["snapshot_date"], r["lowest_price"]) for r in result["rows"]] == [
        ("2026-10-01", 10.0), ("2026-10-15", 12.0), ("2026-11-01", 15.0)
    ]


def test_release_history_counts_back_from_the_latest_snapshot(data, reader):
    rows = run_tool(reader, "release_history", {"release_id": 101, "days": 20})["rows"]

    assert [r["snapshot_date"] for r in rows] == ["2026-10-15", "2026-11-01"]


def test_release_history_of_an_unknown_release(data, reader):
    result = run_tool(reader, "release_history", {"release_id": 999})

    assert result["release"] is None and result["rows"] == []


# --- market_changes ------------------------------------------------------------

def test_biggest_price_rises(data, reader):
    result = run_tool(reader, "market_changes", {"metric": "lowest_price", "days": 31})

    assert [(r["release_id"], r["start_value"], r["end_value"], r["change_pct"]) for r in result["rows"]] == [
        (101, 10.0, 15.0, 50.0),
        # 103 has no price on 1 Nov (sold out): its last known price counts.
        (103, 40.0, 45.0, 12.5),
    ]


def test_biggest_price_falls(data, reader):
    rows = run_tool(reader, "market_changes", {"metric": "lowest_price", "direction": "down", "days": 31})["rows"]

    assert [(r["release_id"], r["change"], r["change_pct"]) for r in rows] == [(102, -5.0, -16.7)]


def test_demand_rises_with_a_filter(data, reader):
    rows = run_tool(reader, "market_changes", {"metric": "want", "days": 31, "artist": "craig"})["rows"]

    assert [(r["release_id"], r["start_value"], r["end_value"]) for r in rows] == [(103, 80, 95)]


def test_a_period_with_a_single_day_has_no_changes(data, reader):
    assert run_tool(reader, "market_changes", {"metric": "lowest_price", "days": 10})["rows"] == []


# --- compare_labels ------------------------------------------------------------

def test_compare_labels(data, reader):
    rows = run_tool(reader, "compare_labels", {"labels": ["planet e", "underground"]})["rows"]

    assert [r["label"] for r in rows] == ["Planet E", "Planet Earth Records", "Underground Resistance"]
    planet_e = rows[0]
    assert planet_e["vinyl_releases"] == 3
    assert planet_e["releases_on_sale"] == 2
    assert planet_e["copies_for_sale"] == 16
    assert planet_e["average_lowest_price"] == 20.0
    assert planet_e["median_lowest_price"] == 20.0
    assert (planet_e["total_want"], planet_e["total_have"]) == (545, 750)
    # Weighted by votes: (4.5*50 + 4.8*120 + 4.0*3) / 173
    assert planet_e["average_rating"] == 4.7
    assert planet_e["as_of"] == "2026-11-01"
    assert rows[1]["average_rating"] is None  # nobody rated its only release


# --- safety ----------------------------------------------------------------------

@pytest.mark.parametrize(
    "name, arguments, problem",
    [
        ("drop_tables", {}, "Unknown tool"),
        ("search_releases", {"limit": MAX_LIMIT + 1}, "limit"),
        ("search_releases", {"limit": 0}, "limit"),
        ("search_releases", {"year_from": 99999}, "year_from"),
        ("search_releases", {"year_from": 2000, "year_to": 1990}, "year_from must not be after year_to"),
        ("search_releases", {"title": "   "}, "title"),
        ("search_releases", {"title": "x" * 101}, "title"),
        ("search_releases", {"sql": "SELECT 1"}, "sql"),
        ("rank_releases", {"sort_by": "id; DROP TABLE releases"}, "sort_by"),
        ("rank_releases", {}, "sort_by"),
        ("release_history", {"release_id": "abc"}, "release_id"),
        ("release_history", {"release_id": 1, "days": 1000}, "days"),
        ("market_changes", {"metric": "password"}, "metric"),
        ("compare_labels", {"labels": []}, "labels"),
        ("compare_labels", {"labels": ["a", "b", "c", "d", "e", "f"]}, "labels"),
    ],
)
def test_invalid_calls_are_refused_with_a_clear_reason(reader, name, arguments, problem):
    with pytest.raises(ToolError) as error:
        run_tool(reader, name, arguments)
    assert problem in str(error.value)


def test_sql_in_a_text_argument_is_only_text(data, reader, db):
    result = run_tool(reader, "search_releases", {"title": "'; DROP TABLE releases; --"})

    assert result["rows"] == []
    assert db.execute("SELECT count(*) FROM releases").fetchone() == (5,)


@pytest.mark.parametrize("text", ["%", "_", "100%"])
def test_wildcards_typed_by_the_user_match_literally(data, reader, text):
    assert ids(run_tool(reader, "search_releases", {"title": text})) == [105]


@pytest.mark.parametrize("name, arguments", [
    ("search_releases", {}),
    ("rank_releases", {"sort_by": "lowest_price"}),
    ("release_history", {"release_id": 101}),
    ("market_changes", {"metric": "lowest_price", "days": 31}),
    ("compare_labels", {"labels": ["planet"]}),
])
def test_every_result_can_be_sent_as_json(data, reader, name, arguments):
    json.dumps(run_tool(reader, name, arguments))


def test_every_tool_has_a_description_and_a_strict_json_schema():
    for tool in TOOLS.values():
        schema = tool.arguments.model_json_schema()
        assert tool.description
        assert schema["additionalProperties"] is False
        assert schema["type"] == "object"
