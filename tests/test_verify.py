"""The number check run on every answer: figures must come from the data the answer was built from."""
import pytest

from queries.questions import FIXED_TEXT
from queries.verify import figures, unverified_figures

SOURCES = [{
    "tool": "rank_releases",
    "arguments": {"sort_by": "lowest_price", "label": "earthtones", "limit": 10},
    "result": {
        "rows": [{
            "title": "Quarter Of A Century, Part 2 of 3", "catno": "ET12-008", "year": 2000,
            "as_of": "2026-11-01", "num_for_sale": 15, "lowest_price": 6.57, "want": 1995, "have": 274,
            "rating_average": 4.33, "change_pct": 12.5,
        }],
        "returned": 1, "total": 23, "truncated": True,
    },
}]


def check(answer, question="Cheapest Earthtones records?", sources=SOURCES):
    return unverified_figures(answer, question, sources, FIXED_TEXT)


@pytest.mark.parametrize("answer", [
    "Quarter Of A Century, Part 2 of 3 (ET12-008, 2000) is cheapest at €6.57 with 15 copies for sale.",
    "It has 1,995 wants and 274 haves, rated 4.33, as of 1 November 2026.",
    "Its price rose 12.5% in the period.",
    "These are the top 10 of 23 matching releases.",
    "The cheapest copy is about €7, around €6.6.",  # rounded from 6.57
    "1. First point\n2. Second point",  # list markers are layout
    "8. Eighth point\n 9) Ninth point",  # numbers that appear nowhere in the data
    "I can look back up to 90 days, or 365 days for changes, and compare up to 5 labels.",  # from the tool descriptions
    "There are 0 copies left.",
    "The 2nd pressing.",
])
def test_figures_from_the_data_pass(answer):
    assert check(answer) == []


@pytest.mark.parametrize("answer, flagged", [
    ("It costs €13.20.", ["13.20"]),
    ("It has 300 wants.", ["300"]),
    ("Prices fell 40% this month.", ["40"]),
    ("It costs €6.58.", ["6.58"]),  # a near miss is not a match
    ("Its want went from 1,995 to 2,100.", ["2,100"]),
])
def test_invented_or_mistyped_figures_are_flagged(answer, flagged):
    assert check(answer) == flagged


def test_a_figure_from_the_question_passes():
    assert check("Here are the changes over the last 45 days.", question="What changed in the last 45 days?") == []


def test_an_answer_without_sources_may_not_contain_data_figures():
    assert check("Records on that label cost about €25.", sources=[]) == ["25"]


def test_numbers_inside_codes_and_words_are_not_figures():
    assert [written for written, _, _ in figures("ET12-008, 24-bit, 2026-11-01, A1, 3x12\"")] == []


RANKED_TABLE = """| {head} | Title | Want |
| --- | --- | --- |
| 77 | Quarter Of A Century | 1,995 |
| 88 | Quarter Of A Century | {want} |
"""


@pytest.mark.parametrize("head", ["#", "Rank", "rank", "No.", "Nr"])
def test_row_numbers_in_a_table_are_layout(head):
    assert check(RANKED_TABLE.format(head=head, want="1,995")) == []


def test_figures_in_a_numbered_table_are_still_checked():
    assert check(RANKED_TABLE.format(head="#", want="4,321")) == ["4,321"]


def test_only_a_column_for_row_numbers_is_left_out():
    assert check(RANKED_TABLE.format(head="Copies", want="1,995")) == ["77", "88"]


def test_a_number_after_a_numbered_table_is_still_checked():
    answer = RANKED_TABLE.format(head="#", want="1,995") + "\nThere are 66 more.\n\n| Copies | Want |\n| --- | --- |\n| 55 | 1,995 |"

    assert check(answer) == ["66", "55"]


def test_a_rank_written_as_a_figure_elsewhere_is_still_checked():
    assert check("| # | Want |\n| --- | --- |\n| 77 | 1,995 |\n\nIts rank was 77 of 88.") == ["77", "88"]
