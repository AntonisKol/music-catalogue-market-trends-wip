"""Check that the figures in an answer come from the data it was based on.

Runs on every visitor's answer, with no extra AI call: every number in the answer
(prices, counts, percentages, years, days) must appear in what the answer was
built from (the rows our tools returned, the arguments they ran with, the
question itself, or the instructions and tool descriptions, such as "up to 90 days").
A figure Claude rounded ("about EUR 17" for 17.14) counts as found; a figure it
invented, mistyped or calculated itself does not. The check cannot tell whether
the words around a number are right; the before-release test covers that.
"""
import re
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation

# A number standing on its own: not part of a word or code such as "ET12-001" or
# "2nd". Thousands separators ("1,995") and decimals ("29.00") are allowed.
NUMBER = re.compile(r"(?<![\w.,\-/])(\d{1,3}(?:,\d{3})+|\d+)(?:\.(\d+))?(?![\w\-/]|\.\d|,\d{3})")
# Numbered list markers at the start of a line ("1. ", "2) ") are layout, not data.
LIST_MARKER = re.compile(r"^\s*\d+[.)]\s", re.MULTILINE)
DIGITS = re.compile(r"\d+(?:\.\d+)?")


def figures(text: str) -> list:
    """The numbers in a text, as (as written, value, decimal places)."""
    text = LIST_MARKER.sub(" ", text)
    found = []
    for match in NUMBER.finditer(text):
        whole, fraction = match.group(1), match.group(2)
        written = whole + (f".{fraction}" if fraction else "")
        value = Decimal(written.replace(",", ""))
        found.append((written, value, len(fraction) if fraction else 0))
    return found


def known_values(*sources) -> set:
    """Every number in the given objects: numeric fields, and digits inside strings and dates."""
    values = set()

    def walk(item):
        if isinstance(item, bool) or item is None:
            return
        if isinstance(item, (int, float, Decimal)):
            try:
                values.add(Decimal(str(item)))
            except InvalidOperation:
                pass
        elif isinstance(item, str):
            values.update(Decimal(digits) for digits in DIGITS.findall(item))
        elif isinstance(item, dict):
            for value in item.values():
                walk(value)
        elif isinstance(item, (list, tuple, set)):
            for value in item:
                walk(value)

    for source in sources:
        walk(source)
    return values


def is_known(value: Decimal, places: int, known: set) -> bool:
    if value == 0 or value in known:
        return True
    step = Decimal(1).scaleb(-places)
    return any(candidate.quantize(step, rounding=ROUND_HALF_UP) == value for candidate in known)


def unverified_figures(answer: str, question: str, sources: list, fixed_text: str = "") -> list:
    """The figures in the answer that appear nowhere in what it was built from, as written."""
    known = known_values(
        [source.get("result") for source in sources],
        [source.get("arguments") for source in sources],
        question,
        fixed_text,
    )
    return [written for written, value, places in figures(answer) if not is_known(value, places, known)]


def fixed_text_of(system_prompt: str, tools: dict) -> str:
    """The instructions and tool descriptions, whose numbers an answer may repeat ("up to 90 days").

    The tools' argument schemas are left out on purpose: their bounds (25, 100, 1900,
    2100, ...) would otherwise let invented prices and counts pass.
    """
    return "\n".join([system_prompt, *(tool.description for tool in tools.values())])
