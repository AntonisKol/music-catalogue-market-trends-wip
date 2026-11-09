"""The only way the question feature reads the database: five fixed query tools.

Each tool has a Pydantic model for its arguments (types, ranges, allowed values),
which validates what the language model sends and gives the JSON schema that
describes the tool to it. The SQL of every tool is written here; arguments only ever
reach it as query parameters. Sort orders and metrics are picked from fixed
lists, never pasted from input. Every result is capped and says how many rows
matched in total, and only vinyl releases are ever returned.
"""
from dataclasses import dataclass
from datetime import date, timedelta
from decimal import Decimal
from typing import Callable, Literal, Optional

import psycopg
from psycopg import sql
from psycopg.rows import dict_row
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator

DEFAULT_LIMIT = 25
MAX_LIMIT = 50
MAX_LABELS_COMPARED = 5


class ToolError(ValueError):
    """The tool was called with a name or arguments it does not accept."""


# --- Arguments -----------------------------------------------------------------

def text_filter(description: str):
    return Field(default=None, min_length=1, max_length=100, description=description)


Limit = Field(default=DEFAULT_LIMIT, ge=1, le=MAX_LIMIT, description=f"Rows to return, at most {MAX_LIMIT}.")


class Arguments(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class ReleaseFilters(Arguments):
    """Narrow the releases. Text filters match any part of the name, ignoring case."""

    title: Optional[str] = text_filter("Part of the release title.")
    label: Optional[str] = text_filter("Part of the label name.")
    artist: Optional[str] = text_filter("Part of an artist name.")
    genre: Optional[str] = text_filter("Part of a Discogs genre, e.g. Electronic.")
    style: Optional[str] = text_filter("Part of a Discogs style, e.g. Techno.")
    country: Optional[str] = text_filter("Part of the release country.")
    year_from: Optional[int] = Field(default=None, ge=1900, le=2100, description="Earliest release year.")
    year_to: Optional[int] = Field(default=None, ge=1900, le=2100, description="Latest release year.")

    @model_validator(mode="after")
    def years_in_order(self):
        if self.year_from and self.year_to and self.year_from > self.year_to:
            raise ValueError("year_from must not be after year_to")
        return self


class SearchReleases(ReleaseFilters):
    limit: int = Limit


class ReleaseHistory(Arguments):
    release_id: int = Field(ge=1, description="Discogs release id, from another tool's result.")
    days: int = Field(default=30, ge=1, le=90, description="How many days back from the latest snapshot. A month back needs at least 31; choose more rather than less. Want, have and ratings are saved once a week, so other days show them as null.")


SortBy = Literal[
    "lowest_price", "highest_price", "most_wanted", "most_owned",
    "best_rated", "fewest_for_sale", "most_wanted_per_owner",
]


class RankReleases(ReleaseFilters):
    sort_by: SortBy = Field(description="What to rank by, using each release's latest snapshot.")
    min_ratings: int = Field(default=5, ge=1, le=1000, description="For best_rated: fewest votes a release needs.")
    limit: int = Limit


class MarketChanges(ReleaseFilters):
    metric: Literal["lowest_price", "num_for_sale", "want", "have"] = Field(description="The value to compare.")
    direction: Literal["up", "down"] = Field(default="up", description="Biggest rises or biggest falls.")
    days: int = Field(default=30, ge=1, le=365, description="Period, counted back from the latest snapshot. A month back needs at least 31; choose more rather than less. Want and have are saved once a week, so compare them over at least 14 days.")
    limit: int = Limit


class CompareLabels(Arguments):
    labels: list[str] = Field(
        min_length=1, max_length=MAX_LABELS_COMPARED,
        description="Label names, or parts of them; each matching label gets its own row.",
    )

    @model_validator(mode="after")
    def names_not_blank(self):
        if any(not name.strip() or len(name) > 100 for name in self.labels):
            raise ValueError("each label name must be 1 to 100 characters")
        return self


# --- SQL building blocks -------------------------------------------------------

# The one place that decides what counts as vinyl in an answer: Discogs lists the
# format "Vinyl" in the release's formats. The label pipeline only saves vinyl,
# but the single-release commands save any release, so every tool applies this.
VINYL = sql.SQL("""r.formats @> '[{"name": "Vinyl"}]'::jsonb""")

# Each release's most recent price, with its most recent want/have/ratings.
# Prices are saved every night and details once a week, so the two dates differ.
# Each is one lookup in its table's (release_id, snapshot_date) index, so the cost
# grows with the number of releases, not with the nights of history.
LATEST = sql.SQL("""
    latest AS (
        SELECT rel.id AS release_id, p.snapshot_date, p.num_for_sale, p.lowest_price, p.currency,
               d.want, d.have, d.rating_count, d.rating_average, d.snapshot_date AS details_date
        FROM releases rel
        CROSS JOIN LATERAL (
            SELECT snapshot_date, num_for_sale, lowest_price, currency FROM price_snapshots
            WHERE release_id = rel.id ORDER BY snapshot_date DESC LIMIT 1
        ) p
        LEFT JOIN LATERAL (
            SELECT snapshot_date, want, have, rating_count, rating_average FROM demand_snapshots
            WHERE release_id = rel.id ORDER BY snapshot_date DESC LIMIT 1
        ) d ON true
    )
""")

ARTISTS = sql.SQL("""
    (SELECT array_agg(a.name ORDER BY a.name)
     FROM release_artists ra JOIN artists a ON a.id = ra.artist_id
     WHERE ra.release_id = r.id) AS artists
""")

FILTERS = {
    "title": sql.SQL("r.title ILIKE %(title)s"),
    # Any label the release was issued on, not only the first one shown (issue 94).
    "label": sql.SQL(
        "EXISTS (SELECT 1 FROM release_labels rl JOIN labels rl_l ON rl_l.id = rl.label_id"
        " WHERE rl.release_id = r.id AND rl_l.name ILIKE %(label)s)"
    ),
    "artist": sql.SQL(
        "EXISTS (SELECT 1 FROM release_artists ra JOIN artists a ON a.id = ra.artist_id"
        " WHERE ra.release_id = r.id AND a.name ILIKE %(artist)s)"
    ),
    "genre": sql.SQL("EXISTS (SELECT 1 FROM unnest(r.genres) g WHERE g ILIKE %(genre)s)"),
    "style": sql.SQL("EXISTS (SELECT 1 FROM unnest(r.styles) s WHERE s ILIKE %(style)s)"),
    "country": sql.SQL("r.country ILIKE %(country)s"),
    "year_from": sql.SQL("r.year >= %(year_from)s"),
    "year_to": sql.SQL("r.year <= %(year_to)s"),
}
TEXT_FILTERS = {"title", "label", "artist", "genre", "style", "country"}

RANKINGS = {
    # sort_by: (only releases where..., order)
    "lowest_price": ("latest.lowest_price IS NOT NULL", "latest.lowest_price ASC"),
    "highest_price": ("latest.lowest_price IS NOT NULL", "latest.lowest_price DESC"),
    "most_wanted": ("latest.want IS NOT NULL", "latest.want DESC"),
    "most_owned": ("latest.have IS NOT NULL", "latest.have DESC"),
    "best_rated": (
        "latest.rating_average IS NOT NULL AND latest.rating_count >= %(min_ratings)s",
        "latest.rating_average DESC, latest.rating_count DESC",
    ),
    "fewest_for_sale": ("latest.num_for_sale IS NOT NULL", "latest.num_for_sale ASC, latest.want DESC NULLS LAST"),
    "most_wanted_per_owner": (
        "latest.want IS NOT NULL AND latest.have > 0",
        "latest.want::numeric / latest.have DESC",
    ),
}

METRICS = {
    "lowest_price": sql.SQL("p.lowest_price"),
    "num_for_sale": sql.SQL("p.num_for_sale"),
    "want": sql.SQL("d.want"),
    "have": sql.SQL("d.have"),
}


def contains(text: str) -> str:
    """A case-insensitive 'contains' pattern; % and _ typed by the user stay literal."""
    escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def filter_clause(args: ReleaseFilters) -> tuple[sql.Composable, dict]:
    clauses, params = [VINYL], {}
    for name, clause in FILTERS.items():
        value = getattr(args, name)
        if value is not None:
            clauses.append(clause)
            params[name] = contains(value) if name in TEXT_FILTERS else value
    return sql.SQL(" AND ").join(clauses), params


def json_safe(value):
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, list):
        return [json_safe(v) for v in value]
    return value


def result(rows: list, **extra) -> dict:
    total = rows[0].pop("total") if rows else 0
    for row in rows[1:]:
        row.pop("total")
    rows = [{key: json_safe(value) for key, value in row.items()} for row in rows]
    return {"rows": rows, "returned": len(rows), "total": total, "truncated": total > len(rows), **extra}


def fetch(conn: psycopg.Connection, query: sql.Composable, params: dict) -> list:
    with conn.cursor(row_factory=dict_row) as cur:
        return cur.execute(query, params).fetchall()


# --- When the data starts ------------------------------------------------------

# Tracking started in November 2026 and labels are added every night, so a period
# asked about can begin before there is any data (issue 95). The tools that cover a
# period say where the data starts, so an answer can say so instead of searching again.
# Each date is one lookup per release in the (release_id, snapshot_date) index, like LATEST.
FIRST_AND_LATEST = sql.SQL("""
    SELECT min(f.snapshot_date) AS first, max(z.snapshot_date) AS latest
    FROM releases rel
    CROSS JOIN LATERAL (SELECT snapshot_date FROM price_snapshots
                        WHERE release_id = rel.id ORDER BY snapshot_date LIMIT 1) f
    CROSS JOIN LATERAL (SELECT snapshot_date FROM price_snapshots
                        WHERE release_id = rel.id ORDER BY snapshot_date DESC LIMIT 1) z
""")
RELEASE_FIRST = sql.SQL("""
    SELECT wanted.release_id,
           (SELECT min(p.snapshot_date) FROM price_snapshots p WHERE p.release_id = wanted.release_id)
    FROM unnest(%(ids)s::int[]) AS wanted(release_id)
""")


def first_snapshots(conn: psycopg.Connection, release_ids: list) -> dict:
    """Each release's first snapshot date."""
    if not release_ids:
        return {}
    return dict(conn.execute(RELEASE_FIRST, {"ids": list(release_ids)}).fetchall())


def coverage(data_starts: Optional[date], period_starts: Optional[date]) -> dict:
    """Where the data and the period start, with a note when the data starts later."""
    extra = {"data_starts": json_safe(data_starts), "period_starts": json_safe(period_starts)}
    if data_starts and period_starts and data_starts > period_starts:
        extra["note"] = (
            f"The data starts on {data_starts.isoformat()}, after the start of the period asked for"
            f" ({period_starts.isoformat()}), so it covers only part of that period."
        )
    return extra


def with_data_starts(conn: psycopg.Connection, found: dict, days: int) -> dict:
    """A market_changes result, with where the data starts overall and, per row, where it starts later."""
    first, latest = conn.execute(FIRST_AND_LATEST).fetchone()
    later = first_snapshots(conn, [row["release_id"] for row in found["rows"]])
    for row in found["rows"]:
        start = later.get(row["release_id"])
        if start and first and start > first:
            row["data_starts"] = start.isoformat()
    found.update(coverage(first, latest - timedelta(days=days) if latest else None))
    return found


# --- The tools -----------------------------------------------------------------

RELEASE_COLUMNS = sql.SQL("""
    r.id AS release_id, r.title, {artists}, l.name AS label, r.catno, r.year, r.country,
    r.genres, r.styles,
    latest.snapshot_date AS as_of, latest.num_for_sale, latest.lowest_price, latest.currency,
    latest.details_date AS demand_as_of, latest.want, latest.have, latest.rating_count, latest.rating_average
""").format(artists=ARTISTS)


def search_releases(conn: psycopg.Connection, args: SearchReleases) -> dict:
    where, params = filter_clause(args)
    query = sql.SQL("""
        WITH {latest}
        SELECT {columns}, count(*) OVER () AS total
        FROM releases r
        LEFT JOIN labels l ON l.id = r.label_id
        LEFT JOIN latest ON latest.release_id = r.id
        WHERE {where}
        ORDER BY r.year NULLS LAST, r.title, r.id
        LIMIT %(limit)s
    """).format(latest=LATEST, columns=RELEASE_COLUMNS, where=where)
    return result(fetch(conn, query, {**params, "limit": args.limit}))


def rank_releases(conn: psycopg.Connection, args: RankReleases) -> dict:
    where, params = filter_clause(args)
    condition, order = RANKINGS[args.sort_by]
    query = sql.SQL("""
        WITH {latest}
        SELECT {columns}, count(*) OVER () AS total
        FROM releases r
        LEFT JOIN labels l ON l.id = r.label_id
        JOIN latest ON latest.release_id = r.id
        WHERE {where} AND {condition}
        ORDER BY {order}, r.id
        LIMIT %(limit)s
    """).format(latest=LATEST, columns=RELEASE_COLUMNS, where=where,
                condition=sql.SQL(condition), order=sql.SQL(order))
    params.update(limit=args.limit, min_ratings=args.min_ratings)
    return result(fetch(conn, query, params), sort_by=args.sort_by)


def release_history(conn: psycopg.Connection, args: ReleaseHistory) -> dict:
    release = fetch(conn, sql.SQL("""
        SELECT r.id AS release_id, r.title, {artists}, l.name AS label, r.year
        FROM releases r LEFT JOIN labels l ON l.id = r.label_id
        WHERE r.id = %(release_id)s AND {vinyl}
    """).format(artists=ARTISTS, vinyl=VINYL), {"release_id": args.release_id})
    if not release:
        return {"release": None, "rows": [], "returned": 0, "total": 0, "truncated": False}
    query = sql.SQL("""
        SELECT p.snapshot_date, p.num_for_sale, p.lowest_price, p.currency,
               d.want, d.have, d.rating_count, d.rating_average, count(*) OVER () AS total
        FROM price_snapshots p
        LEFT JOIN demand_snapshots d USING (release_id, snapshot_date)
        WHERE p.release_id = %(release_id)s
          AND p.snapshot_date > (SELECT max(snapshot_date) FROM price_snapshots
                                 WHERE release_id = %(release_id)s) - %(days)s
        ORDER BY p.snapshot_date
    """)
    rows = fetch(conn, query, {"release_id": args.release_id, "days": args.days})
    header = {key: json_safe(value) for key, value in release[0].items()}
    period_starts = rows[-1]["snapshot_date"] - timedelta(days=args.days - 1) if rows else None
    starts = coverage(first_snapshots(conn, [args.release_id]).get(args.release_id), period_starts)
    return result(rows, release=header, **starts)


def market_changes(conn: psycopg.Connection, args: MarketChanges) -> dict:
    where, params = filter_clause(args)
    direction, order = {
        "up": (sql.SQL("ends.end_value > ends.start_value"), sql.SQL("change_pct DESC NULLS LAST, change DESC")),
        "down": (sql.SQL("ends.end_value < ends.start_value"), sql.SQL("change_pct ASC NULLS LAST, change ASC")),
    }[args.direction]
    query = sql.SQL("""
        WITH series AS (
            SELECT p.release_id, p.snapshot_date, {metric} AS value
            FROM price_snapshots p
            LEFT JOIN demand_snapshots d USING (release_id, snapshot_date)
            WHERE p.snapshot_date >= (SELECT max(snapshot_date) FROM price_snapshots) - %(days)s
        ), ends AS (
            SELECT release_id,
                   (array_agg(snapshot_date ORDER BY snapshot_date) FILTER (WHERE value IS NOT NULL))[1] AS start_date,
                   (array_agg(value ORDER BY snapshot_date) FILTER (WHERE value IS NOT NULL))[1] AS start_value,
                   (array_agg(snapshot_date ORDER BY snapshot_date DESC) FILTER (WHERE value IS NOT NULL))[1] AS end_date,
                   (array_agg(value ORDER BY snapshot_date DESC) FILTER (WHERE value IS NOT NULL))[1] AS end_value
            FROM series GROUP BY release_id
        )
        SELECT r.id AS release_id, r.title, {artists}, l.name AS label, r.year,
               ends.start_date, ends.start_value, ends.end_date, ends.end_value,
               ends.end_value - ends.start_value AS change,
               CASE WHEN ends.start_value <> 0
                    THEN round((ends.end_value - ends.start_value) * 100.0 / ends.start_value, 1) END AS change_pct,
               count(*) OVER () AS total
        FROM ends
        JOIN releases r ON r.id = ends.release_id
        LEFT JOIN labels l ON l.id = r.label_id
        WHERE ends.end_date > ends.start_date AND {direction} AND {where}
        ORDER BY {order}, r.id
        LIMIT %(limit)s
    """).format(metric=METRICS[args.metric], artists=ARTISTS, direction=direction, where=where, order=order)
    params.update(days=args.days, limit=args.limit)
    found = result(fetch(conn, query, params), metric=args.metric, direction=args.direction)
    return with_data_starts(conn, found, args.days)


def compare_labels(conn: psycopg.Connection, args: CompareLabels) -> dict:
    query = sql.SQL("""
        WITH {latest}
        SELECT l.id AS label_id, l.name AS label,
               count(r.id) AS vinyl_releases,
               count(latest.lowest_price) AS releases_on_sale,
               sum(latest.num_for_sale) AS copies_for_sale,
               round(avg(latest.lowest_price), 2) AS average_lowest_price,
               round(percentile_cont(0.5) WITHIN GROUP (ORDER BY latest.lowest_price)::numeric, 2)
                   AS median_lowest_price,
               sum(latest.want) AS total_want,
               sum(latest.have) AS total_have,
               round(sum(latest.rating_average * latest.rating_count)
                     / nullif(sum(latest.rating_count) FILTER (WHERE latest.rating_average IS NOT NULL), 0), 2)
                   AS average_rating,
               max(latest.snapshot_date) AS as_of,
               max(latest.details_date) AS demand_as_of,
               count(*) OVER () AS total
        FROM labels l
        JOIN release_labels rl ON rl.label_id = l.id
        JOIN releases r ON r.id = rl.release_id AND {vinyl}
        LEFT JOIN latest ON latest.release_id = r.id
        WHERE l.name ILIKE ANY (%(patterns)s)
        GROUP BY l.id, l.name
        ORDER BY l.name, l.id
        LIMIT %(limit)s
    """).format(latest=LATEST, vinyl=VINYL)
    limit = 2 * MAX_LABELS_COMPARED
    rows = fetch(conn, query, {"patterns": [contains(name) for name in args.labels], "limit": limit})
    return result(rows)


# --- Registry ------------------------------------------------------------------

@dataclass(frozen=True)
class Tool:
    name: str
    description: str
    arguments: type[Arguments]
    run: Callable[[psycopg.Connection, Arguments], dict]


# Told to the model with every tool that returns prices, so answers say what the
# number is and do not overstate a change.
PRICE_NOTE = (
    " Prices are the cheapest copy on sale in any condition, without shipping, in EUR"
    " (converted by Discogs from the seller's currency)."
)

TOOLS = {tool.name: tool for tool in [
    Tool(
        "search_releases",
        "Find vinyl releases in the catalogue by title, label, artist, genre, style, country or year, "
        "with each release's latest price and demand." + PRICE_NOTE,
        SearchReleases, search_releases,
    ),
    Tool(
        "rank_releases",
        "Rank vinyl releases by their latest snapshot: lowest or highest price, "
        "most wanted, most owned, best rated, fewest copies for sale, or most wanted per owner. "
        "Optionally narrowed by label, artist, genre, style, country or year." + PRICE_NOTE,
        RankReleases, rank_releases,
    ),
    Tool(
        "release_history",
        "Day-by-day price, supply and demand of one vinyl release, for up to 90 days back from its latest "
        "snapshot. data_starts is the release's first snapshot; a note says when that is after the period's start."
        + PRICE_NOTE,
        ReleaseHistory, release_history,
    ),
    Tool(
        "market_changes",
        "Vinyl releases whose lowest price, copies for sale, want or have changed most over a period "
        "(up to 365 days back from the latest snapshot), rising or falling. data_starts is the catalogue's "
        "first snapshot, also given on a row whose release starts later; a note says when the data starts "
        "after the period's start." + PRICE_NOTE +
        " A change in the lowest price can mean the cheapest copy sold, or a change in exchange rates.",
        MarketChanges, market_changes,
    ),
    Tool(
        "compare_labels",
        "Compare up to 5 labels: number of vinyl releases, copies for sale, average and median lowest price, "
        "total want and have, and average rating, from each release's latest snapshot." + PRICE_NOTE,
        CompareLabels, compare_labels,
    ),
]}


def run_tool(conn: psycopg.Connection, name: str, arguments: dict) -> dict:
    """Validate the arguments for a named tool and run it."""
    tool = TOOLS.get(name)
    if tool is None:
        raise ToolError(f"Unknown tool {name!r}; available: {', '.join(TOOLS)}")
    try:
        args = tool.arguments.model_validate(arguments)
    except ValidationError as error:
        problems = "; ".join(
            f"{'.'.join(str(p) for p in e['loc']) or 'arguments'}: {e['msg']}" for e in error.errors()
        )
        raise ToolError(f"Invalid arguments for {name}: {problems}") from error
    try:
        return tool.run(conn, args)
    except psycopg.errors.QueryCanceled as error:
        # The reader's 5 s statement limit stopped the query.
        raise ToolError(
            f"{name} took too long and was stopped; narrow it with more filters or a shorter period"
        ) from error
