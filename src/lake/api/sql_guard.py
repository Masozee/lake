"""Read-only SQL validation.

This module is the *second* line of defence, not the first. The first is the
engine itself: `lake.api.engine` opens the serving replica with

    read_only=True                 -> no INSERT/UPDATE/DELETE/CREATE/DROP
    enable_external_access=False   -> no read_csv, read_parquet, ATTACH, INSTALL,
                                      COPY ... TO, and it cannot be re-enabled
                                      at runtime once the database is open.

Everything below would still hold if this file were deleted. What it buys is a
fast, legible rejection with a good error message, instead of a confusing
PermissionException from deep inside DuckDB — and a defence that survives someone
later "temporarily" opening the connection read-write.

Facts established by probing DuckDB 1.5, each of which shaped a rule here:

* `duckdb.extract_statements()` runs the real parser, so statement classification
  is not regex guesswork. Multi-statement input comes back as a list.
* `SELECT * FROM read_csv('/etc/passwd')` classifies as StatementType.SELECT.
  A statement-type allowlist ALONE is an arbitrary-file-read hole.
* `PRAGMA database_list` also classifies as SELECT, and leaks the absolute path
  of the database file.
* `SET enable_external_access=true` fails once the DB is open. The lock is real.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

import duckdb

# Statement types a reader may execute. DuckDB's own parser assigns these.
# Everything absent is rejected: COPY, ATTACH, DETACH, LOAD/INSTALL, SET,
# CREATE, DROP, ALTER, INSERT, UPDATE, DELETE, TRANSACTION, CALL, PREPARE, ...
_ALLOWED_TYPES = frozenset(
    {
        duckdb.StatementType.SELECT,
        duckdb.StatementType.EXPLAIN,
    }
)

# These parse as SELECT but are not selects. PRAGMA database_list returns the
# on-disk path of every attached database; pragma_* table functions do the same.
# CALL invokes table functions directly.
_LEADING_KEYWORD_DENY = re.compile(
    r"^\s*(?:pragma|call|attach|detach|install|load|set|reset|export|import)\b",
    re.IGNORECASE,
)

# Table functions that touch the filesystem or the network. `enable_external_access`
# already blocks every one of these at the engine, so this list is belt-and-braces:
# it turns a PermissionException into a clear "function not allowed" message, and
# it keeps holding if someone loosens the engine config.
_DENIED_FUNCTIONS = frozenset(
    {
        "read_csv",
        "read_csv_auto",
        "read_parquet",
        "read_json",
        "read_json_auto",
        "read_ndjson",
        "read_ndjson_auto",
        "read_text",
        "read_blob",
        "parquet_scan",
        "csv_scan",
        "json_scan",
        "glob",
        "sniff_csv",
        "delta_scan",
        "iceberg_scan",
        "postgres_scan",
        "sqlite_scan",
        "mysql_scan",
        "duckdb_settings",
        "duckdb_extensions",
        "pragma_database_list",
        "pragma_database_size",
        "pragma_table_info",
        "pragma_version",
        "pragma_platform",
        "getenv",
        "which_secret",
        "shell",
    }
)

# `foo(` at a word boundary, not preceded by a dot (so `t.read_csv` — a column —
# is not a false positive) and not inside a longer identifier.
_FUNCTION_CALL = re.compile(r"(?<![\w.])([a-zA-Z_][a-zA-Z0-9_]*)\s*\(")

MAX_QUERY_BYTES = 16_384


class UnsafeQuery(ValueError):
    """The query is rejected. The message is safe to show a caller."""


@dataclass(frozen=True, slots=True)
class ValidatedQuery:
    """A single statement that the parser agrees is a SELECT or an EXPLAIN."""

    sql: str
    statement_type: str


def _strip_comments(sql: str) -> str:
    """Remove comments so they cannot hide a denied function name.

    `SELECT 1 /* read_csv( */` must not trip the function scan, and
    `SELECT 1 -- ;DROP` must not confuse statement counting.
    """
    sql = re.sub(r"/\*.*?\*/", " ", sql, flags=re.DOTALL)
    sql = re.sub(r"--[^\n]*", " ", sql)
    return sql


def _mask_string_literals(sql: str) -> str:
    """Blank out '...' and "..." so literals cannot smuggle a function name.

    `SELECT 'read_csv(' AS x` is a perfectly legal, harmless query.
    """
    sql = re.sub(r"'(?:''|[^'])*'", "''", sql)
    sql = re.sub(r'"(?:""|[^"])*"', '""', sql)
    return sql


def validate(sql: str, *, connection: duckdb.DuckDBPyConnection | None = None) -> ValidatedQuery:
    """Return the single safe statement, or raise UnsafeQuery.

    `connection` is used only to reach DuckDB's parser. It is never executed
    against, and a read-only connection is the right thing to pass.
    """
    if not sql or not sql.strip():
        raise UnsafeQuery("empty query")

    if len(sql.encode("utf-8")) > MAX_QUERY_BYTES:
        raise UnsafeQuery(f"query exceeds {MAX_QUERY_BYTES} bytes")

    cleaned = _strip_comments(sql)

    if _LEADING_KEYWORD_DENY.match(cleaned):
        keyword = cleaned.strip().split(None, 1)[0].upper()
        raise UnsafeQuery(f"{keyword} is not permitted; only SELECT and EXPLAIN are")

    # The parser, not a regex, decides what these statements are.
    parser = connection or duckdb.connect(":memory:")
    try:
        statements = parser.extract_statements(sql)
    except duckdb.Error as exc:
        raise UnsafeQuery(f"could not parse: {exc}") from exc
    finally:
        if connection is None:
            parser.close()

    if len(statements) == 0:
        raise UnsafeQuery("empty query")
    if len(statements) > 1:
        # Blocks `SELECT 1; DROP TABLE t` even though the engine would reject the
        # second statement anyway. One request, one statement.
        raise UnsafeQuery(f"expected one statement, got {len(statements)}")

    statement = statements[0]
    if statement.type not in _ALLOWED_TYPES:
        name = str(statement.type).rsplit(".", 1)[-1]
        raise UnsafeQuery(f"{name} is not permitted; only SELECT and EXPLAIN are")

    # SELECT * FROM read_csv('/etc/passwd') got this far: the parser calls it a
    # SELECT. Scan for filesystem and network table functions, ignoring anything
    # inside a string literal or a comment.
    scannable = _mask_string_literals(cleaned).lower()
    for name in _FUNCTION_CALL.findall(scannable):
        if name in _DENIED_FUNCTIONS:
            raise UnsafeQuery(f"function {name}() is not permitted")
        if name.startswith("pragma_") or name.startswith("duckdb_"):
            raise UnsafeQuery(f"function {name}() is not permitted")

    return ValidatedQuery(sql=sql.strip(), statement_type=str(statement.type).rsplit(".", 1)[-1])
