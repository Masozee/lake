"""HTTP surface, against the real read-only engine.

The attack tests matter most: they assert that no request, however phrased, gets a
200 with data it should not have — and that no file is written to disk.
"""

from __future__ import annotations

import json

import pytest

pytestmark = pytest.mark.integration


# -- catalog ------------------------------------------------------------------


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok", "tables": 1}


def test_list_tables(client):
    assert client.get("/api/tables").json() == ["gdp_annual"]


def test_describe_table(client):
    body = client.get("/api/tables/gdp_annual").json()
    assert body["row_count"] == 5
    names = {c["name"] for c in body["columns"]}
    assert {"country_iso3", "gdp_usd", "year"} <= names


def test_describe_unknown_table_is_404(client):
    assert client.get("/api/tables/secrets").status_code == 404


def test_profile_lists_distinct_values(client):
    profile = client.get("/api/tables/gdp_annual/profile").json()["profile"]
    iso = next(c for c in profile if c["column_name"] == "country_iso3")
    assert set(iso["distinct_values"]) == {"IDN", "USA", "DEU"}


# -- query --------------------------------------------------------------------


def test_aggregation_query(client):
    r = client.post(
        "/api/query",
        json={"sql": "SELECT country_iso3, sum(gdp_usd) g FROM lake.gdp_annual GROUP BY 1"},
    )
    assert r.status_code == 200
    body = r.json()
    totals = dict(body["rows"])
    assert totals["USA"] == pytest.approx(5.69e13)
    assert totals["DEU"] is None  # NULL preserved, not coerced


def test_query_row_limit_and_truncation_flag(client):
    r = client.post("/api/query", json={"sql": "SELECT * FROM lake.gdp_annual", "limit": 2})
    body = r.json()
    assert body["row_count"] == 2
    assert body["truncated"] is True


def test_stream_returns_ndjson(client):
    r = client.post(
        "/api/query/stream",
        json={"sql": "SELECT country_iso3, year FROM lake.gdp_annual ORDER BY 1, 2"},
    )
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("application/x-ndjson")

    lines = [json.loads(line) for line in r.text.strip().splitlines()]
    assert lines[0] == {"columns": ["country_iso3", "year"]}
    assert ["DEU", 2024] in lines[1:]
    assert len(lines) == 6  # header + 5 rows


# -- attacks ------------------------------------------------------------------

WRITES = [
    "INSERT INTO lake.gdp_annual VALUES ('X', 'x', 2024, 1.0)",
    "UPDATE lake.gdp_annual SET gdp_usd = 0",
    "DELETE FROM lake.gdp_annual",
    "DROP TABLE lake.gdp_annual",
    "CREATE TABLE evil AS SELECT * FROM lake.gdp_annual",
    "ALTER TABLE lake.gdp_annual RENAME TO x",
]

ESCAPES = [
    "SELECT * FROM read_csv('/etc/passwd')",
    "SELECT * FROM read_parquet('/etc/hosts')",
    "COPY (SELECT 1) TO '/tmp/lake_exfil_test.csv'",
    "ATTACH '/tmp/evil.db' AS evil",
    "INSTALL httpfs",
    "PRAGMA database_list",
    "SELECT getenv('LAKE_ANTHROPIC_API_KEY')",
]


@pytest.mark.parametrize("sql", WRITES)
def test_writes_are_rejected(client, sql):
    r = client.post("/api/query", json={"sql": sql})
    assert r.status_code == 422
    assert "not permitted" in r.json()["detail"]


@pytest.mark.parametrize("sql", ESCAPES)
def test_filesystem_and_disclosure_are_rejected(client, sql):
    r = client.post("/api/query", json={"sql": sql})
    assert r.status_code == 422


def test_multiple_statements_are_rejected(client):
    r = client.post("/api/query", json={"sql": "SELECT 1; DROP TABLE lake.gdp_annual"})
    assert r.status_code == 422
    assert "one statement" in r.json()["detail"]


def test_a_copy_attack_writes_no_file(client, tmp_path):
    target = tmp_path / "exfil.csv"
    r = client.post("/api/query", json={"sql": f"COPY (SELECT 1) TO '{target}'"})
    assert r.status_code == 422
    assert not target.exists()


def test_the_table_survives_the_attacks(client):
    """After every write attempt, the data is untouched."""
    for sql in WRITES:
        client.post("/api/query", json={"sql": sql})
    assert client.get("/api/tables/gdp_annual").json()["row_count"] == 5


def test_streaming_endpoint_also_rejects_writes(client):
    r = client.post("/api/query/stream", json={"sql": "DELETE FROM lake.gdp_annual"})
    assert r.status_code == 422


# -- rate limiting ------------------------------------------------------------


def test_query_tier_returns_429_after_the_limit(client):
    """The 31st query in a window is throttled; health is never limited."""
    # default query limit is 30/min. Exhaust it.
    codes = []
    for _ in range(32):
        r = client.post("/api/query", json={"sql": "SELECT 1"})
        codes.append(r.status_code)

    assert codes.count(200) == 30
    assert 429 in codes
    # the throttled response tells the client when to retry
    r = client.post("/api/query", json={"sql": "SELECT 1"})
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_health_is_never_rate_limited(client):
    for _ in range(200):
        assert client.get("/api/health").status_code == 200
