"""Rows and aggregates over the serving replica — the public read surface.

There is no SQL endpoint. A reader asks for the rows of a *thing* — a dataset, a
statistical table inside one, or a single series — and the shape of that request is
a URL:

    GET /api/data/i5demefo/rows?period=gte:2000

`i5demefo` is the same id the thing's page is addressed by. It is a hash of the keys
behind it, so it survives a rebuild, and it saves the reader from spelling those keys
out: the alternative to that URL is

    ?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29

which nobody wants to type, escape, or paste into a paper. Filters compose on top —
an id fixes the slice, and a filter narrows within it.

The reason there is no SQL is not that SQL was dangerous here. The engine is
read-only with the filesystem off, and it never was. It is that a query language is a
contract, and this one is smaller: a caller names a thing, a column, an operator from
a fixed list, and a value. Nothing they send is ever a fragment of SQL.

Two rules make that true, and they are the same two `admin.browse` runs on:

* **Identifiers** — an id, and every column named in a filter, a sort, or a
  projection, is looked up in the real catalog, and the catalog's own copy is what
  reaches the query. An unknown one raises; an injected one cannot survive the round
  trip.
* **Values** — every value is a bound parameter. Not quoted, not escaped: bound.

`admin.browse` is this same machinery behind a session, returning what a *grid* needs
(constant columns, pinned filters). This module is the plain one: it returns what a
client needs.
"""

from __future__ import annotations

from typing import Any, Literal

from lake.api import catalog
from lake.api.catalog import describe_table
from lake.api.engine import SCHEMA, jsonable, read_cursor, scalar

#: Rows per request. The ceiling is high because a client pulling data is not a
#: browser rendering it — but it is a ceiling, so one request cannot stream forever.
DEFAULT_LIMIT = 1_000
MAX_LIMIT = 100_000

#: Rows an export may carry. Larger than a JSON page: the point of an export is to
#: take the data away, and it streams rather than materialising.
EXPORT_MAX_ROWS = 1_000_000

QUERY_TIMEOUT = 30.0
EXPORT_TIMEOUT = 120.0

#: Groups an aggregate may return. An aggregation with more buckets than this is
#: not a summary, it is the table again — ask for rows instead.
MAX_GROUPS = 10_000

#: The comparisons a filter may use. An allowlist, so `op` can never be a fragment
#: of SQL a caller chose.
Operator = Literal[
    "eq", "ne", "contains", "starts", "gt", "lt", "gte", "lte", "in", "null", "notnull"
]

OPERATORS: frozenset[str] = frozenset(
    {"eq", "ne", "contains", "starts", "gt", "lt", "gte", "lte", "in", "null", "notnull"}
)

#: Operators that mean something for any column, because they compare it as text or
#: not at all. Everything else needs a column you can order.
_TEXT_OPS = frozenset({"eq", "ne", "contains", "starts", "in", "null", "notnull"})

#: The aggregate functions a caller may name. An allowlist for the same reason: the
#: function name is written into the SQL text, so it may only ever be one of these.
AGGREGATES: frozenset[str] = frozenset({"count", "sum", "avg", "min", "max", "median"})

#: Which DuckDB types are worth comparing numerically rather than as text.
_NUMERIC = ("INT", "DEC", "DOUBLE", "FLOAT", "REAL", "HUGEINT", "NUMERIC", "BIGINT")
_TEMPORAL = ("DATE", "TIME")

#: What makes a sort TOTAL, appended to every ORDER BY.
#:
#: LIMIT/OFFSET slices an order, and rows that compare equal may come back in either
#: order between one request and the next — so with a non-total sort a row can appear
#: on both page 1 and page 2 while another appears on neither. The reader sees a
#: duplicate and a hole and cannot tell that the data is fine.
_TIEBREAK = ("dataset_id", "group_id", "series", "row_no", "period")

#: What a page is sorted by when the caller has not chosen. Every row in the lake is
#: an observation at a `period`, and newest-first is what someone reading a series
#: wants. Without it, rows arrive in the order Parquet happens to hold them.
DEFAULT_SORT = "period"


class BadRequest(ValueError):
    """A filter, sort, or projection the API does not serve. Safe to show a caller."""


# -- parsing the query string -------------------------------------------------


def parse_filter(column: str, raw: str) -> dict[str, Any]:
    """`period=gte:2000` -> {"column": "period", "op": "gte", "value": "2000"}.

    A value with no operator means equality, which is what a reader writing
    `?freq=annual` by hand expects. The split is on the FIRST colon only, so a value
    may contain one: `?updated=gte:2024-01-01T00:00:00` works.
    """
    op, sep, value = raw.partition(":")
    if not sep or op not in OPERATORS:
        # No prefix, or a prefix that is not an operator ("10:30" is a time, not an
        # op). The whole string is the value and the comparison is equality.
        return {"column": column, "op": "eq", "value": raw}
    return {"column": column, "op": op, "value": value}


# -- what an id addresses -----------------------------------------------------


def pinned(thing_id: str) -> tuple[str, list[dict[str, Any]]]:
    """An id, as the table to read and the filters that isolate it.

        observations   -> ("observations", [])                  the raw table
        i5demefo       -> (..., [dataset_id])                        a dataset
        4qkxwlbo       -> (..., [dataset_id, group_id])                a group
        d3zsbcue       -> (..., [dataset_id, group_id, series])       a series

    Every source lands in one table, so a dataset *is* a filtered view of it, and a
    series is a narrower one. That is what lets one id address any rung: the id is a
    hash of the keys, and the keys are the filter.

    It is also what makes an id worth having. Spelling the keys out in a URL means
    `?dataset_id=seki_indicators&group_id=I.1.&series=Uang+Beredar+Luas%28M2%29`, and
    the reader who wants that series does not want to type any of it.

    The raw table is the one thing addressed by name rather than by id, because it is
    not a dataset — it is what all of them are views of.

    The id is resolved against the real catalog, and what gets pinned is the catalog's
    own copy of each key, never the caller's string. That lookup is the defence: an id
    naming nothing raises `KeyError` here rather than reaching the query.
    """
    if thing_id == catalog.OBSERVATIONS:
        return catalog.OBSERVATIONS, []

    thing = catalog.resolve(thing_id)  # raises KeyError on an unknown id

    pins: list[dict[str, Any]] = [
        {"column": catalog.DATASET_COLUMN, "op": "eq", "value": thing.dataset_id}
    ]
    if thing.group_id is not None:
        pins.append({"column": catalog.GROUP_COLUMN, "op": "eq", "value": thing.group_id})
    if thing.series is not None:
        pins.append({"column": catalog.SERIES_COLUMN, "op": "eq", "value": thing.series})

    return catalog.OBSERVATIONS, pins


# -- compiling to SQL ---------------------------------------------------------


def _columns_of(table: str) -> dict[str, str]:
    """The table's real columns, keyed by name.

    `describe_table` resolves the table against the catalog and raises KeyError on an
    unknown one — so this is also where an unknown *table* is caught.
    """
    return {c.name: c.type.upper() for c in describe_table(table).columns}


def _resolve(name: str, columns: dict[str, str]) -> str:
    """A caller's column name, replaced by the catalog's own copy of it.

    Never interpolate the caller's string. Look it up, and use what the catalog gives
    back — an injected identifier cannot survive that.
    """
    if name not in columns:
        raise BadRequest(f"unknown column {name!r}. known: {', '.join(sorted(columns))}")
    return name


def _is_numeric(duck_type: str) -> bool:
    return any(t in duck_type for t in _NUMERIC)


def _comparable(duck_type: str) -> bool:
    """Can `>` and `<` mean anything for this column?"""
    return _is_numeric(duck_type) or any(t in duck_type for t in _TEMPORAL)


def _escape_like(value: str) -> str:
    r"""Neutralise LIKE's own wildcards inside a caller's search text.

    Someone searching for "50%" means the literal characters, not "starts with 50".
    DuckDB's ILIKE treats \ as the escape character by default.
    """
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _is_temporal(duck_type: str) -> bool:
    return any(t in duck_type for t in _TEMPORAL)


def _compare(column: str, duck_type: str, symbol: str, value: Any) -> tuple[str, Any]:
    """One ordered comparison, as a clause and its bound parameter.

    A number arrives from the query string as text and DuckDB will cast the bound
    parameter itself, so the only job is to reject what would otherwise surface as a
    confusing engine error.

    A date is the interesting case. `?period=gte:2020` plainly means "2020 onward",
    but `period` is a DATE and DuckDB will not cast "2020" to one — it wants
    YYYY-MM-DD and raises otherwise. Demanding a full date would be pedantry: the
    caller's meaning is obvious and the lake is full of partial periods anyway. So a
    temporal column is compared as text, which is *correct* rather than merely
    convenient, because an ISO-8601 date sorts identically as a string and as a date.
    That is the whole reason the format is written biggest-unit-first.
    """
    if _is_numeric(duck_type):
        try:
            return f'"{column}" {symbol} ?', float(value)
        except (TypeError, ValueError) as exc:
            raise BadRequest(f"{value!r} is not a number") from exc

    if _is_temporal(duck_type):
        return f'CAST("{column}" AS VARCHAR) {symbol} ?', str(value)

    return f'"{column}" {symbol} ?', value


def _where(filters: list[dict[str, Any]], columns: dict[str, str]) -> tuple[str, list[Any]]:
    """Compile the filters into a WHERE clause plus its bound parameters.

    Returns ("", []) when there is nothing to filter, so the caller drops the keyword
    entirely rather than emitting `WHERE TRUE`.
    """
    clauses: list[str] = []
    params: list[Any] = []

    for spec in filters:
        column = _resolve(str(spec.get("column", "")), columns)
        op = str(spec.get("op", "eq"))
        value = spec.get("value")
        duck_type = columns[column]

        if op == "null":
            clauses.append(f'"{column}" IS NULL')
            continue
        if op == "notnull":
            clauses.append(f'"{column}" IS NOT NULL')
            continue

        if op not in OPERATORS:
            raise BadRequest(f"unknown operator {op!r}. known: {', '.join(sorted(OPERATORS))}")
        if op not in _TEXT_OPS and not _comparable(duck_type):
            raise BadRequest(f"{column} is {duck_type.lower()}; it cannot be compared with {op!r}")

        if op == "eq":
            clauses.append(f'CAST("{column}" AS VARCHAR) = ?')
            params.append(str(value))
        elif op == "ne":
            clauses.append(f'CAST("{column}" AS VARCHAR) <> ?')
            params.append(str(value))
        elif op == "contains":
            # Case-insensitive substring. The wildcards go in the *parameter*, so a
            # value containing % or _ is matched literally rather than as a pattern.
            clauses.append(f'CAST("{column}" AS VARCHAR) ILIKE ?')
            params.append(f"%{_escape_like(str(value))}%")
        elif op == "starts":
            clauses.append(f'CAST("{column}" AS VARCHAR) ILIKE ?')
            params.append(f"{_escape_like(str(value))}%")
        elif op == "in":
            # `?freq=in:annual,quarterly`. An empty list would compile to `IN ()`,
            # which is a syntax error, so it becomes the falsehood it means.
            values = [v for v in str(value).split(",") if v]
            if not values:
                clauses.append("FALSE")
                continue
            holes = ", ".join("?" for _ in values)
            clauses.append(f'CAST("{column}" AS VARCHAR) IN ({holes})')
            params.extend(values)
        else:  # gt, lt, gte, lte — the allowlist above guarantees this is one of them
            symbol = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}[op]
            clause, param = _compare(column, duck_type, symbol, value)
            clauses.append(clause)
            params.append(param)

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def _order(sort: str | None, descending: bool, columns: dict[str, str]) -> str:
    """A TOTAL ORDER BY — see `_TIEBREAK`. Paging without one is broken paging."""
    tiebreak = ", ".join(f'"{c}"' for c in _TIEBREAK if c in columns)

    if sort:
        column = _resolve(sort, columns)
        direction = "DESC" if descending else "ASC"
        # NULLS LAST both ways: a column of mostly-nulls sorted descending should show
        # the values, not a screen of empty cells.
        keys = [f'"{column}" {direction} NULLS LAST']
    elif DEFAULT_SORT in columns:
        keys = [f'"{DEFAULT_SORT}" DESC']
    else:
        return f" ORDER BY {tiebreak}" if tiebreak else ""

    if tiebreak:
        keys.append(tiebreak)
    return f" ORDER BY {', '.join(keys)}"


def _select(select: list[str] | None, columns: dict[str, str]) -> str:
    """The projection. Every name is resolved against the catalog before it is written."""
    if not select:
        return "*"
    return ", ".join(f'"{_resolve(c, columns)}"' for c in select)


# -- the two reads ------------------------------------------------------------


def _compile_rows(
    thing_id: str,
    *,
    select: list[str] | None,
    filters: list[dict[str, Any]],
    sort: str | None,
    descending: bool,
    limit: int,
    offset: int,
) -> tuple[str, list[Any], str, list[Any], dict[str, str], str]:
    """The rows query and the count query that pages it, both fully bound.

    Shared by the JSON read and the exports so there is exactly one place that knows
    how a request becomes SQL — an export that filtered differently from the page it
    was downloaded from would be a quiet, undetectable lie.

    The reader's filters compose with the thing's own, and go down the same binding
    path: a pinned filter is not privileged, just pre-set.
    """
    table, pins = pinned(thing_id)  # raises KeyError on an unknown id

    columns = _columns_of(table)  # raises KeyError with no replica
    name = describe_table(table).name  # the catalog's own copy of the name

    projection = _select(select, columns)
    where, params = _where([*pins, *filters], columns)
    order = _order(sort, descending, columns)

    count_sql = f'SELECT count(*) FROM {SCHEMA}."{name}"{where}'
    # LIMIT/OFFSET are bound too. They are ints here, but binding them keeps the rule
    # "no caller value is ever in the SQL text" without an exception to remember.
    rows_sql = f'SELECT {projection} FROM {SCHEMA}."{name}"{where}{order} LIMIT ? OFFSET ?'
    return rows_sql, [*params, limit, offset], count_sql, params, columns, name


def rows(
    thing_id: str,
    *,
    select: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    sort: str | None = None,
    descending: bool = False,
    limit: int = DEFAULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """One page of a thing's rows, filtered and sorted by the database.

    `thing_id` is the raw table (`observations`) or the short id of a dataset, a
    statistical table inside one, or a single series.

    `total` is the count *after* filtering, because that is the number a pager needs
    — "page 3 of 41" is a lie if the 41 counts rows the filter removed.
    """
    limit = max(1, min(limit, MAX_LIMIT))
    offset = max(0, offset)

    rows_sql, rows_params, count_sql, count_params, _, table = _compile_rows(
        thing_id,
        select=select,
        filters=filters or [],
        sort=sort,
        descending=descending,
        limit=limit,
        offset=offset,
    )

    with read_cursor(QUERY_TIMEOUT) as cursor:
        total = scalar(cursor.execute(count_sql, count_params))
        result = cursor.execute(rows_sql, rows_params)
        headers = [d[0] for d in (result.description or [])]
        data = [[jsonable(v) for v in row] for row in result.fetchall()]

    return {
        "id": thing_id,
        "table": table,
        "columns": headers,
        "rows": data,
        "row_count": len(data),
        "total": total,
        "limit": limit,
        "offset": offset,
        # Whether asking again with a larger offset would return anything. A client
        # paging a million rows should not have to do this arithmetic itself.
        "has_more": offset + len(data) < total,
    }


def aggregate(
    thing_id: str,
    *,
    group_by: list[str],
    measure: str | None = None,
    agg: str = "count",
    filters: list[dict[str, Any]] | None = None,
    sort: str | None = None,
    descending: bool = True,
    limit: int = 100,
) -> dict[str, Any]:
    """A GROUP BY, without a query language.

    `agg` names a function from a fixed list and `measure` names a column from the
    catalog, so the only two things written into the SQL text are both drawn from
    sets this module controls. Everything else is bound.

    `count` is the one aggregate that needs no measure — `count(*)` over the group.
    Every other one is meaningless without a column to aggregate, and asking for
    `sum` of nothing is a mistake worth reporting rather than defaulting away.

    Aggregating *inside* a thing is the useful case: the total by year within one
    series, rather than across the whole lake.
    """
    if agg not in AGGREGATES:
        raise BadRequest(f"unknown aggregate {agg!r}. known: {', '.join(sorted(AGGREGATES))}")
    if not group_by:
        raise BadRequest("group_by is required; ask for rows if you do not want groups")

    table, pins = pinned(thing_id)  # raises KeyError on an unknown id

    columns = _columns_of(table)
    name = describe_table(table).name

    keys = [_resolve(c, columns) for c in group_by]

    if agg == "count" and measure is None:
        expression = "count(*)"
        label = "count"
    else:
        if measure is None:
            raise BadRequest(f"{agg} needs a measure column")
        column = _resolve(measure, columns)
        if agg in ("sum", "avg", "median") and not _is_numeric(columns[column]):
            raise BadRequest(f"{column} is {columns[column].lower()}; it cannot be {agg}med")
        # `agg` is from AGGREGATES and `column` from the catalog — neither is the
        # caller's string, which is the only reason this interpolation is safe.
        expression = f'{agg}("{column}")'
        label = f"{agg}_{column}"

    # The thing's own filters, then the caller's. Same binding path for both.
    where, params = _where([*pins, *(filters or [])], columns)

    grouping = ", ".join(f'"{k}"' for k in keys)

    # Sorting an aggregate means sorting by the measure — that is what makes a bar
    # chart a ranking rather than an alphabet. A caller may instead name a grouping
    # column. The tiebreak is the grouping itself, which is unique by construction.
    if sort is None or sort == label:
        order_key = expression
    else:
        order_key = f'"{_resolve(sort, columns)}"'
        if sort not in keys:
            raise BadRequest(f"cannot sort by {sort!r}: it is not grouped or aggregated")
    direction = "DESC" if descending else "ASC"

    limit = max(1, min(limit, MAX_GROUPS))

    sql = (
        f'SELECT {grouping}, {expression} AS "{label}" '
        f'FROM {SCHEMA}."{name}"{where} '
        f"GROUP BY {grouping} "
        f"ORDER BY {order_key} {direction} NULLS LAST, {grouping} "
        f"LIMIT ?"
    )

    with read_cursor(QUERY_TIMEOUT) as cursor:
        result = cursor.execute(sql, [*params, limit])
        headers = [d[0] for d in (result.description or [])]
        data = [[jsonable(v) for v in row] for row in result.fetchall()]

    return {
        "id": thing_id,
        "table": name,
        "group_by": keys,
        "measure": label,
        "columns": headers,
        "rows": data,
        "row_count": len(data),
        "limit": limit,
        # Whether the ranking is complete or a top-N of something longer. A bar chart
        # captioned "the ten biggest" is right; one captioned "all of them" is not.
        "truncated": len(data) == limit,
    }


# -- export -------------------------------------------------------------------


def export_sql(
    thing_id: str,
    *,
    select: list[str] | None = None,
    filters: list[dict[str, Any]] | None = None,
    sort: str | None = None,
    descending: bool = False,
    limit: int = EXPORT_MAX_ROWS,
    offset: int = 0,
) -> tuple[str, list[Any]]:
    """The same read as `rows`, as SQL for the streaming exporters.

    A file is the data, so `limit` defaults to everything rather than to a page — but
    it is a *default*, not a rule: a caller who asks for `?limit=100` gets a hundred
    rows in their CSV, because they said so.

    Shares `_compile_rows` with `rows`, so an export cannot filter differently from the
    JSON page it was downloaded from. That would be a quiet, undetectable lie.
    """
    limit = max(1, min(limit, EXPORT_MAX_ROWS))
    sql, params, _, _, _, _ = _compile_rows(
        thing_id,
        select=select,
        filters=filters or [],
        sort=sort,
        descending=descending,
        limit=limit,
        offset=max(0, offset),
    )
    return sql, params


def default_filename(thing_id: str) -> str:
    """What a download of this thing should be called.

    `observations.csv` for the raw table, and the thing's own title otherwise — a
    reader who downloads the M2 series wants `M2.csv` in their downloads folder, not
    another `observations.csv` beside the four they already have.
    """
    if thing_id == catalog.OBSERVATIONS:
        return catalog.OBSERVATIONS
    try:
        thing = catalog.resolve(thing_id)
    except KeyError:
        return thing_id
    return thing.series or thing.group_id or thing.dataset_id
