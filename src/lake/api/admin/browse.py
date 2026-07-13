"""Paged, sorted, filtered reads over the serving replica — for the Data browser.

The whole point of this module is that the *database* does the work. `seki_indicators`
holds 970,700 rows; a browser that fetches them all to sort them in JavaScript is
not slow, it is broken. So the page, the sort, and the filters are compiled into
one SQL query, and DuckDB returns the twenty-five rows the screen actually shows.

Nothing a caller sends is ever interpolated into that SQL.

* **Identifiers** — the table and every column named in a sort or filter are looked
  up in the real catalog, and the catalog's own copy of the name is what gets
  written into the query. A name that is not there raises; an injected one cannot
  survive the round trip even if quoting were somehow bypassed.
* **Values** — every filter value is a bound parameter. Not quoted, not escaped:
  bound. There is no string of user input anywhere in the SQL text.

The engine underneath is read-only with external access disabled, so even a
successful injection could not write, attach, or read a file. This is the layer
that means one never gets that far.
"""

from __future__ import annotations

from typing import Any, Literal

from lake.api import rows
from lake.api.catalog import describe_table
from lake.api.engine import SCHEMA, jsonable, read_cursor, scalar

#: Rows per page. A ceiling, not a suggestion: the browser renders every row it is
#: given, and a "page" of 10,000 is a hung tab.
DEFAULT_PAGE_SIZE = 25
MAX_PAGE_SIZE = 200

#: How long a browse query may run. Generous — a filter across a million rows on a
#: NUC's disk is not instant — but bounded, so one bad filter cannot pin the CPU.
TIMEOUT_SECONDS = 30.0

#: What a page is sorted by when the reader has not chosen. Every row in the lake is an
#: observation at a `period`, and newest-first is what someone opening a series wants to
#: see. Without it, rows come back in the order Parquet happens to hold them — which for
#: a time series looks like corruption.
DEFAULT_SORT = "period"

#: What makes a sort TOTAL, appended to every ORDER BY.
#:
#: This is not decoration. LIMIT/OFFSET slices an order, and rows that compare equal may
#: come back in either order between one query and the next — so with a non-total sort a
#: row can appear on both page 1 and page 2 while another appears on neither. The reader
#: sees a duplicate and a hole and has no way to know the data is fine.
#:
#: Together these five identify a row uniquely — verified across all 987,860 of them.
#:
#: `row_no` has to be in here, and the reason is a bug this lake has not fixed yet:
#: `(dataset_id, group_id, series, period)` is NOT unique. SEKI's table I.17. lists
#: "Tabungan" once per bank group — under Bank Persero, under Bank Swasta Nasional, and
#: so on — so twenty-two different lines of numbers share that name, and the row's real
#: identity is its position in the printed table. Eighty-four of SEKI's 108 tables are
#: hierarchical like this. Until `series` is made to carry the indent path, `row_no` is
#: what tells those rows apart, and a page of them cannot be ordered without it.
_TIEBREAK = ("dataset_id", "group_id", "series", "row_no", "period")

#: The comparisons a column filter may use. An allowlist, so `op` can never be a
#: fragment of SQL a caller chose.
Operator = Literal["contains", "equals", "gt", "lt", "gte", "lte", "empty", "not_empty"]

_TEXT_OPS = {"contains", "equals", "empty", "not_empty"}

#: Which DuckDB types are worth comparing numerically rather than as text.
_NUMERIC = ("INT", "DEC", "DOUBLE", "FLOAT", "REAL", "HUGEINT", "NUMERIC", "BIGINT")
_TEMPORAL = ("DATE", "TIME")


class BadFilter(ValueError):
    """A filter that names a column we do not have, or an operator we do not run."""


def _column_map(table: str) -> dict[str, str]:
    """The table's real columns, keyed by name.

    `describe_table` resolves the table against the catalog and raises KeyError on
    an unknown one — so this is also where an unknown *table* is caught.
    """
    return {c.name: c.type.upper() for c in describe_table(table).columns}


def _resolve(column: str, columns: dict[str, str]) -> str:
    """A caller's column name, replaced by the catalog's own copy of it.

    Never interpolate the caller's string. Look it up, and use what the catalog
    gives back — an injected identifier cannot survive that.
    """
    if column not in columns:
        known = ", ".join(sorted(columns))
        raise BadFilter(f"unknown column {column!r}. known: {known}")
    return column


def _is_numeric(duck_type: str) -> bool:
    return any(t in duck_type for t in _NUMERIC)


def _comparable(duck_type: str) -> bool:
    """Can `>` and `<` mean anything for this column?"""
    return _is_numeric(duck_type) or any(t in duck_type for t in _TEMPORAL)


def _where(filters: list[dict[str, Any]], columns: dict[str, str]) -> tuple[str, list[Any]]:
    """Compile the filters into a WHERE clause plus its bound parameters.

    Returns ("", []) when there is nothing to filter, so the caller can drop the
    keyword entirely rather than emit `WHERE TRUE`.
    """
    clauses: list[str] = []
    params: list[Any] = []

    for spec in filters:
        column = _resolve(str(spec.get("column", "")), columns)
        op = str(spec.get("op", "contains"))
        value = spec.get("value")
        duck_type = columns[column]

        if op == "empty":
            clauses.append(f'"{column}" IS NULL')
            continue
        if op == "not_empty":
            clauses.append(f'"{column}" IS NOT NULL')
            continue

        if op not in _TEXT_OPS and not _comparable(duck_type):
            raise BadFilter(f"{column} is {duck_type.lower()}; it cannot be compared with {op!r}")

        if op == "contains":
            # Case-insensitive substring. The wildcards go in the *parameter*, so a
            # value containing % or _ is matched literally rather than as a pattern.
            clauses.append(f'CAST("{column}" AS VARCHAR) ILIKE ?')
            params.append(f"%{_escape_like(str(value))}%")
        elif op == "equals":
            clauses.append(f'CAST("{column}" AS VARCHAR) = ?')
            params.append(str(value))
        elif op in ("gt", "lt", "gte", "lte"):
            symbol = {"gt": ">", "lt": "<", "gte": ">=", "lte": "<="}[op]
            clauses.append(f'"{column}" {symbol} ?')
            params.append(_coerce(value, duck_type))
        else:
            raise BadFilter(f"unknown operator {op!r}")

    if not clauses:
        return "", []
    return " WHERE " + " AND ".join(clauses), params


def _escape_like(value: str) -> str:
    r"""Neutralise LIKE's own wildcards inside a user's search text.

    Someone searching for "50%" means the literal characters, not "starts with 50".
    DuckDB's ILIKE treats \ as the escape character by default.
    """
    return value.replace("\\", "\\\\").replace("%", r"\%").replace("_", r"\_")


def _coerce(value: Any, duck_type: str) -> Any:
    """Make a JSON value comparable against a typed column.

    A number arrives from JSON as a number, but a date arrives as a string; DuckDB
    casts the parameter itself, so the job here is only to reject the nonsense that
    would otherwise become a confusing engine error.
    """
    if _is_numeric(duck_type):
        try:
            return float(value)
        except (TypeError, ValueError) as exc:
            raise BadFilter(f"{value!r} is not a number") from exc
    return value


def _pinned(thing_id: str) -> tuple[str, list[dict[str, Any]]]:
    """Turn an id into the table to read and the filters that isolate it.

    `lake.api.rows.pinned` is the one place that knows this, because the public API is
    keyed the same way — `/api/data/{id}/rows` and this grid resolve the same ids to
    the same filters, and two copies of that rule would eventually disagree about what
    an id means.

    The only difference is the operator's name: this module has always called equality
    `equals`, and the public one calls it `eq` because that is what a reader types in a
    URL. Same comparison, translated at the boundary.
    """
    table, pins = rows.pinned(thing_id)  # raises KeyError on an unknown id
    return table, [{**pin, "op": "equals"} for pin in pins]


def browse(
    thing_id: str,
    *,
    page: int = 0,
    size: int = DEFAULT_PAGE_SIZE,
    sort: str | None = None,
    descending: bool = False,
    filters: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """One page of a dataset, sorted and filtered by the database.

    `thing_id` is the raw table (`observations`) or the short id of a dataset, a
    statistical table inside one, or a single series. Every source lands in one
    table, so all of them are filtered views of it and the filter is applied here
    rather than being something the reader has to know to type.

    `total` is the count *after* filtering, because that is the number the pager
    needs — "page 3 of 41" is a lie if the 41 counts rows the filter removed.
    """
    table, pins = _pinned(thing_id)

    columns = _column_map(table)  # raises KeyError with no replica
    name = describe_table(table).name  # the catalog's own copy of the name

    # An id that resolves but selects nothing must raise, not quietly return an
    # empty grid: a stale id and a dataset with no rows look identical otherwise,
    # and only one of them is a 404. Checking it is a count, and the pins are bound.
    if pins:
        exists_where, exists_params = _where(pins, columns)
        with read_cursor(TIMEOUT_SECONDS) as cursor:
            found = scalar(
                cursor.execute(
                    f'SELECT count(*) FROM {SCHEMA}."{name}"{exists_where}', exists_params
                )
            )
        if not found:
            raise KeyError(f"no dataset {thing_id!r}")

    # The reader's filters compose with the dataset's own, and go down the same
    # binding path — a pinned filter is not privileged, just pre-set.
    where, params = _where([*pins, *(filters or [])], columns)

    # A sort has to be TOTAL, or paging is broken. Two rows that compare equal may come
    # back in either order, and LIMIT/OFFSET slices that order — so a row can appear on
    # both page 1 and page 2 while another appears on neither. Every sort below
    # therefore ends in a tiebreak that no two rows can share.
    tiebreak = ", ".join(f'"{c}"' for c in _TIEBREAK if c in columns)

    if sort:
        column = _resolve(sort, columns)
        direction = "DESC" if descending else "ASC"
        # NULLS LAST both ways: a column of mostly-nulls sorted descending should
        # show the values, not a screen of empty cells.
        keys = [f'"{column}" {direction} NULLS LAST']
        if tiebreak:
            keys.append(tiebreak)
        order = f" ORDER BY {', '.join(keys)}"
    elif DEFAULT_SORT in columns:
        # Newest first, by default. Parquet gives rows back in the order they were
        # written, which for a time series is neither chronological nor stable — page 1
        # of M2 opened on February 2001, September 2001, October 2001, and a reader
        # cannot tell whether that is the data or the display. Every row in this table
        # is an observation at a `period`, so there is always something to sort by.
        keys = [f'"{DEFAULT_SORT}" DESC']
        if tiebreak:
            keys.append(tiebreak)
        order = f" ORDER BY {', '.join(keys)}"
    else:
        order = f" ORDER BY {tiebreak}" if tiebreak else ""

    size = max(1, min(size, MAX_PAGE_SIZE))
    page = max(0, page)
    offset = page * size

    with read_cursor(TIMEOUT_SECONDS) as cursor:
        total = scalar(cursor.execute(f'SELECT count(*) FROM {SCHEMA}."{name}"{where}', params))

        constant = _constant_columns(cursor, name, columns, where, params) if pins else {}

        # LIMIT/OFFSET are bound too — they are ints here, but binding them keeps
        # the rule "no caller value is ever in the SQL text" without exception.
        result = cursor.execute(
            f'SELECT * FROM {SCHEMA}."{name}"{where}{order} LIMIT ? OFFSET ?',
            [*params, size, offset],
        )
        headers = [d[0] for d in (result.description or [])]
        rows = [[jsonable(v) for v in row] for row in result.fetchall()]

    return {
        "id": thing_id,
        "table": name,
        # Which columns the dataset pins. Filtering `group_id` inside a group that IS
        # one `group_id` can only ever return everything or nothing.
        "pinned": [p["column"] for p in pins],
        # Columns that hold the same value on every row of this view, and what that
        # value is. The UI folds them out of the grid and says so — see below.
        "constant": constant,
        "columns": [{"name": c, "type": columns[c]} for c in headers],
        "rows": rows,
        "total": total,
        "page": page,
        "size": size,
        "pages": (total + size - 1) // size if size else 0,
    }


def _constant_columns(
    cursor: Any,
    table: str,
    columns: dict[str, str],
    where: str,
    params: list[Any],
) -> dict[str, Any]:
    """The columns that never vary inside this view, and the value they hold.

    Browsing one series, four columns repeat the same string 304 times: `dataset_id`,
    `group_id`, `group_title`, `section`. They are not wrong, they are just not *rows*
    — they are facts about the view, and showing them per-row pushes `period` and
    `value`, the only two columns anyone came for, off the right edge of the screen.

    So the grid folds them away and the page states them once. Which columns those are
    is asked of the database rather than assumed: `group_title` is constant because it
    is determined by `group_id`, and hard-coding that relationship here would be a
    second copy of the schema, quietly rotting.

    Counted over the whole filtered view, not the page: a 25-row page can be constant
    by coincidence, and folding a column away on page 1 that reappears on page 2 is
    worse than never folding it at all.
    """
    if not columns:
        return {}

    # One scan, one row back: how many distinct values each column takes, and one of
    # them. `count(DISTINCT x)` ignores NULLs, so an all-NULL column comes back as 0
    # — which is still constant, and its value is NULL.
    parts = ", ".join(f'count(DISTINCT "{c}"), any_value("{c}"), count("{c}")' for c in columns)
    row = cursor.execute(
        f'SELECT count(*), {parts} FROM {SCHEMA}."{table}"{where}', params
    ).fetchone()

    rows_total = row[0]
    if not rows_total:
        return {}

    out: dict[str, Any] = {}
    for i, column in enumerate(columns):
        distinct, value, filled = row[1 + i * 3], row[2 + i * 3], row[3 + i * 3]
        if distinct == 0:
            out[column] = None  # every row is NULL
        elif distinct == 1 and filled == rows_total:
            out[column] = jsonable(value)  # one value, and no NULLs beside it
    return out
