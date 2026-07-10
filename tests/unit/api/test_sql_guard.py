"""The SQL guard. Every attack here was verified against a real DuckDB first.

If any of these regress, an AI agent with query access can read /etc/passwd or
learn the absolute path of the database file.
"""

from __future__ import annotations

import pytest

from lake.api.sql_guard import MAX_QUERY_BYTES, UnsafeQuery, validate

# -- what a reader is allowed to do -------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "SELECT 1",
        "SELECT * FROM lake.gdp_annual",
        "SELECT country_iso3, sum(gdp_usd) FROM lake.gdp_annual GROUP BY 1 ORDER BY 2 DESC",
        "WITH recent AS (SELECT * FROM lake.gdp_annual WHERE year > 2020) SELECT * FROM recent",
        "SELECT * FROM lake.gdp_annual LIMIT 10",
        "EXPLAIN SELECT * FROM lake.gdp_annual",
        "  select 1  ",
        "SELECT 'read_csv(' AS harmless_literal",  # a denied name inside a string
        "SELECT 1 /* a comment mentioning read_csv( */",
        "SELECT 1 -- read_csv(\n",
    ],
)
def test_allows_reads(sql):
    assert validate(sql).sql


def test_reports_the_statement_type():
    assert validate("SELECT 1").statement_type == "SELECT"
    assert validate("EXPLAIN SELECT 1").statement_type == "EXPLAIN"


# -- writes -------------------------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        "INSERT INTO lake.gdp_annual VALUES ('X', 2024, 1.0)",
        "UPDATE lake.gdp_annual SET gdp_usd = 0",
        "DELETE FROM lake.gdp_annual",
        "DROP TABLE lake.gdp_annual",
        "CREATE TABLE evil (a INTEGER)",
        "CREATE TABLE evil AS SELECT * FROM lake.gdp_annual",
        "ALTER TABLE lake.gdp_annual RENAME TO x",
        "TRUNCATE lake.gdp_annual",
        "CREATE VIEW v AS SELECT 1",
    ],
)
def test_rejects_writes(sql):
    with pytest.raises(UnsafeQuery, match="not permitted"):
        validate(sql)


# -- filesystem and network escapes -------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # VERIFIED: DuckDB's parser calls this a SELECT. A statement-type
        # allowlist alone is an arbitrary-file-read vulnerability.
        "SELECT * FROM read_csv('/etc/passwd')",
        "SELECT * FROM read_csv_auto('/etc/shadow')",
        "SELECT * FROM read_parquet('/mnt/nas/lake/raw/**/*.parquet')",
        "SELECT * FROM read_json('/etc/hosts')",
        "SELECT * FROM read_text('/etc/passwd')",
        "SELECT * FROM read_blob('/etc/passwd')",
        "SELECT * FROM glob('/**')",
        "SELECT * FROM parquet_scan('/tmp/x.parquet')",
        "SELECT * FROM sqlite_scan('/tmp/a.db', 't')",
        "SELECT * FROM postgres_scan('host=x', 'public', 't')",
        "SELECT getenv('LAKE_DB_DSN')",
        "WITH x AS (SELECT * FROM read_csv('/etc/passwd')) SELECT * FROM x",
        "SELECT (SELECT count(*) FROM read_csv('/etc/passwd'))",
    ],
)
def test_rejects_filesystem_access(sql):
    with pytest.raises(UnsafeQuery, match="not permitted"):
        validate(sql)


@pytest.mark.parametrize(
    "sql",
    [
        "COPY (SELECT 1) TO '/tmp/exfil.csv'",
        "COPY lake.gdp_annual TO '/tmp/x.parquet'",
        "ATTACH '/tmp/evil.db' AS evil",
        "DETACH lake",
        "INSTALL httpfs",
        "LOAD httpfs",
        "EXPORT DATABASE '/tmp/dump'",
        "IMPORT DATABASE '/tmp/dump'",
    ],
)
def test_rejects_attach_copy_and_extensions(sql):
    with pytest.raises(UnsafeQuery, match="not permitted"):
        validate(sql)


# -- information disclosure ---------------------------------------------------


@pytest.mark.parametrize(
    "sql",
    [
        # VERIFIED: parses as SELECT, and returns the absolute path of the
        # database file on disk.
        "PRAGMA database_list",
        "pragma database_list",
        "  PRAGMA  version",
        "SELECT * FROM pragma_database_list()",
        "SELECT * FROM duckdb_settings()",
        "SELECT * FROM duckdb_extensions()",
        "CALL pragma_version()",
        "SELECT * FROM pragma_table_info('gdp_annual')",
    ],
)
def test_rejects_introspection_that_leaks_paths(sql):
    with pytest.raises(UnsafeQuery, match="not permitted"):
        validate(sql)


# -- statement smuggling ------------------------------------------------------


def test_rejects_multiple_statements():
    with pytest.raises(UnsafeQuery, match="one statement"):
        validate("SELECT 1; DROP TABLE lake.gdp_annual")


def test_rejects_a_write_hidden_after_a_comment():
    with pytest.raises(UnsafeQuery, match="one statement"):
        validate("SELECT 1 -- harmless\n; DELETE FROM lake.gdp_annual")


def test_rejects_settings_changes():
    """`SET enable_external_access=true` is refused by the engine too, but say so early."""
    with pytest.raises(UnsafeQuery, match="not permitted"):
        validate("SET enable_external_access=true")


# -- resource bounds ----------------------------------------------------------


def test_rejects_an_oversized_query():
    with pytest.raises(UnsafeQuery, match="exceeds"):
        validate("SELECT " + "1," * MAX_QUERY_BYTES)


@pytest.mark.parametrize("sql", ["", "   ", "\n\t"])
def test_rejects_empty(sql):
    with pytest.raises(UnsafeQuery, match="empty"):
        validate(sql)


def test_rejects_unparseable():
    with pytest.raises(UnsafeQuery, match="could not parse"):
        validate("SELECT FROM WHERE ((((")


# -- false positives ----------------------------------------------------------


def test_a_column_named_like_a_denied_function_is_fine():
    """`t.glob` is a column reference, not a call to glob()."""
    validate("SELECT t.glob FROM lake.t AS t")


def test_a_denied_name_inside_a_string_literal_is_fine():
    validate("SELECT * FROM lake.t WHERE name = 'read_parquet('")
