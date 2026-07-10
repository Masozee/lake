"""The tools an AI agent may call. Every one is read-only, by construction.

There is no `edit`, `insert`, `delete`, or `write` tool here, and there is no way
to add one that would work: the underlying connection is opened read-only with
filesystem access disabled (see `lake.api.engine`). Even if a tool tried to
mutate, DuckDB would refuse. The model literally has no verb for it.

`run_sql` is the powerful one, so it is the most defended:

    1. sql_guard.validate()  — parser-level: SELECT/EXPLAIN only, one statement,
                               no file functions, no PRAGMA, no SET.
    2. the read-only engine  — would reject a write or a file read regardless.
    3. row and time ceilings — a model cannot ask for the whole table or hang.

The tool results are plain data. Nothing about the host, the filesystem, or the
process is ever returned to the model.
"""

from __future__ import annotations

from typing import Any

from lake.api import catalog, engine
from lake.api.sql_guard import UnsafeQuery, validate

# Anthropic tool schemas. Kept small and explicit; a model follows a tight schema
# more reliably than a clever one.
TOOL_DEFINITIONS: list[dict[str, Any]] = [
    {
        "name": "list_tables",
        "description": (
            "List every table available in the read-only data lake. Call this "
            "first to see what data exists."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "describe_table",
        "description": (
            "Get the columns, types, and row count of one table. Use before "
            "writing a query so you reference real column names."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string", "description": "table name"}},
            "required": ["table"],
        },
    },
    {
        "name": "profile_table",
        "description": (
            "Per-column statistics for a table: min, max, null count, approximate "
            "distinct count, and — for low-cardinality columns — the list of actual "
            "distinct values. Use this to learn valid filter values before querying."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"table": {"type": "string"}},
            "required": ["table"],
        },
    },
    {
        "name": "run_sql",
        "description": (
            "Run a read-only SQL query against the lake and return rows. Only a "
            "single SELECT or EXPLAIN is permitted. Writes, file access "
            "(read_csv/read_parquet), ATTACH, PRAGMA, and multiple statements are "
            "rejected. Tables live in the `lake` schema, e.g. `lake.gdp_annual`. "
            "Results are capped; add your own LIMIT and aggregate to explore large "
            "tables. This is DuckDB SQL."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "sql": {"type": "string", "description": "a single SELECT statement"},
                "limit": {
                    "type": "integer",
                    "description": "max rows to return (default 1000, hard cap 10000)",
                },
            },
            "required": ["sql"],
        },
    },
]

# What a model may pull back in one call. Smaller than the HTTP ceilings — an
# agent should aggregate, not haul rows into its context window.
AI_DEFAULT_LIMIT = 1_000
AI_MAX_LIMIT = 10_000
AI_TIMEOUT = 15.0


def dispatch(tool_name: str, tool_input: dict[str, Any]) -> dict[str, Any]:
    """Execute one tool call. Returns a JSON-safe dict, never raises to the caller.

    Errors come back as `{"error": "..."}` so the model can read the message and
    correct its next attempt — a rejected query is a normal part of exploration,
    not a crash.
    """
    try:
        if tool_name == "list_tables":
            return {"tables": catalog.list_tables()}

        if tool_name == "describe_table":
            table = catalog.describe_table(_require(tool_input, "table"))
            return {
                "table": table.name,
                "row_count": table.row_count,
                "columns": [
                    {"name": c.name, "type": c.type, "nullable": c.nullable} for c in table.columns
                ],
            }

        if tool_name == "profile_table":
            return {"profile": catalog.column_profile(_require(tool_input, "table"))}

        if tool_name == "run_sql":
            sql = _require(tool_input, "sql")
            limit = min(int(tool_input.get("limit", AI_DEFAULT_LIMIT)), AI_MAX_LIMIT)
            validated = validate(sql, connection=engine.serving())
            return engine.run_query(validated.sql, limit=limit, timeout=AI_TIMEOUT)

        return {"error": f"unknown tool {tool_name!r}"}

    except UnsafeQuery as exc:
        return {"error": f"query rejected: {exc}"}
    except KeyError as exc:
        return {"error": exc.args[0] if exc.args else str(exc)}
    except engine.QueryTimeout as exc:
        return {"error": str(exc)}
    except Exception as exc:  # never leak a traceback to the model
        return {"error": f"{type(exc).__name__}: {exc}"}


def _require(payload: dict[str, Any], key: str) -> Any:
    if key not in payload or payload[key] in (None, ""):
        raise KeyError(f"missing required argument: {key}")
    return payload[key]
