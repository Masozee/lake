"""HTTP surface, against the real read-only engine.

The attack tests matter most: they assert that no request, however phrased, gets a
200 with data it should not have — and that no file is written to disk.

There is no SQL endpoint, so the attacks are the ones this surface actually has: a
caller controls filter values, and names columns, tables, sorts, and aggregates.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


# -- catalog ------------------------------------------------------------------


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    # One: every source lands in the merged `observations` table.
    assert r.json() == {"status": "ok", "tables": 1}


def test_list_tables(client):
    assert client.get("/api/tables").json() == ["observations"]


def test_describe_table(client):
    body = client.get("/api/tables/observations").json()
    assert body["row_count"] == 10  # 5 World Bank rows + 5 SEKI
    names = {c["name"] for c in body["columns"]}
    assert {"dataset_id", "series", "value", "unit", "period"} <= names


def test_describe_unknown_table_is_404(client):
    assert client.get("/api/tables/secrets").status_code == 404


def test_profile_lists_distinct_values(client):
    profile = client.get("/api/tables/observations/profile").json()["profile"]
    code = next(c for c in profile if c["column_name"] == "series_code")
    # SEKI writes NULL here; only the World Bank gives its series a code.
    assert set(code["distinct_values"]) == {"IDN", "USA", "DEU"}


# -- the id -------------------------------------------------------------------
#
# A thing is addressed by the same short id its page is. The id is a hash of the keys
# behind it, so it survives a rebuild — and it saves the reader from spelling those keys
# out, which is the whole argument for having one.


@pytest.fixture
def usa(replica):
    """The id of one series: the United States' GDP.

    Derived, never hard-coded — an id is a hash of the keys, so writing `i5demefo` into
    a test would assert the hash function rather than the behaviour.
    """
    from lake.api import catalog

    return catalog.id_for("gdp_annual", "NY.GDP.MKTP.CD", "United States")


def test_an_id_reads_exactly_the_thing_it_names(client, usa):
    """Two rows, out of ten. The id carries `dataset_id`, `group_id` AND `series` — all
    three, because a series name is not unique across datasets."""
    body = client.get(f"/api/data/{usa}/rows").json()

    assert body["id"] == usa
    assert body["table"] == "observations"
    assert body["total"] == 2
    assert {row[body["columns"].index("series")] for row in body["rows"]} == {"United States"}


def test_a_filter_composes_on_top_of_an_id(client, usa):
    """The id fixes the slice; a filter narrows within it. Not either/or."""
    body = client.get(f"/api/data/{usa}/rows?year=2024").json()

    assert body["total"] == 1  # of the USA's two years, one is 2024


def test_a_filter_cannot_widen_past_an_id(client, usa):
    """A filter is applied *with* the thing's own, never instead of them. Asking for
    Germany's rows inside the USA's series is a contradiction, and the answer is nothing
    — not Germany."""
    body = client.get(f"/api/data/{usa}/rows?series_code=DEU").json()

    assert body["total"] == 0


def test_an_unknown_id_is_a_404_not_an_empty_page(client):
    """A stale id and a thing with no rows look identical otherwise, and only one of
    them is a 404."""
    r = client.get("/api/data/zzzzzzzz/rows")

    assert r.status_code == 404
    assert "zzzzzzzz" in r.json()["detail"]


def test_the_raw_table_is_addressed_by_name(client):
    """It is the one thing that is not a dataset — it is what all of them are views of,
    so it is named rather than hashed."""
    body = client.get("/api/data/observations/rows").json()

    assert body["id"] == "observations"
    assert body["total"] == 10


def test_an_id_is_derived_from_the_keys_not_stored(client, usa):
    """It is a hash of the keys, not a row in a table — which is what lets a link anyone
    shared keep resolving across a rebuild, with no migration and nothing to keep in
    sync. Recomputing it from the keys gives the same id, and it still reads."""
    from lake.api import catalog

    assert catalog.id_for("gdp_annual", "NY.GDP.MKTP.CD", "United States") == usa
    assert client.get(f"/api/data/{usa}/rows").json()["total"] == 2


# -- rows ---------------------------------------------------------------------


def test_rows_returns_a_page_and_the_count_behind_it(client):
    body = client.get("/api/data/observations/rows?limit=2").json()

    assert body["row_count"] == 2
    # `total` is what the filters match, not what came back — a pager needs both, and
    # "page 1 of 5" is unanswerable from a page alone.
    assert body["total"] == 10
    assert body["has_more"] is True


def test_a_filter_narrows_the_rows_and_the_total(client):
    body = client.get("/api/data/observations/rows?series_code=USA").json()

    assert body["total"] == 2  # USA: 2023 and 2024
    assert {row[body["columns"].index("series_code")] for row in body["rows"]} == {"USA"}


def test_an_operator_prefix_compares_rather_than_equals(client):
    """Eight rows are 2024 (three World Bank, five SEKI); two are 2023."""
    body = client.get("/api/data/observations/rows?year=gte:2024&select=year").json()

    assert body["total"] == 8
    assert all(row == [2024] for row in body["rows"])

    # And the comparison is a comparison, not an equality dressed up as one.
    assert client.get("/api/data/observations/rows?year=lt:2024").json()["total"] == 2


def test_a_date_compares_against_a_partial_period(client):
    """`period=gte:2024` plainly means "2024 onward", and DuckDB will not cast "2024"
    to a DATE. An ISO date sorts the same as text, so the comparison is done as text —
    correct, not merely convenient."""
    body = client.get("/api/data/observations/rows?period=gte:2024").json()
    assert body["total"] == 8


def test_a_bare_value_means_equality(client):
    """`?freq=annual` has to do the obvious thing — nobody types `eq:` by hand."""
    prefixed = client.get("/api/data/observations/rows?series_code=eq:USA").json()
    bare = client.get("/api/data/observations/rows?series_code=USA").json()

    assert bare["total"] == prefixed["total"] == 2


def test_select_projects_only_the_named_columns(client):
    body = client.get("/api/data/observations/rows?select=series_code,value&limit=1").json()
    assert body["columns"] == ["series_code", "value"]


def test_a_null_is_a_null_and_not_a_zero(client):
    """SEKI writes no series_code. A missing value is a gap, not a number."""
    body = client.get("/api/data/observations/rows?series_code=null:&select=value").json()

    assert body["total"] == 5  # the five SEKI rows
    assert all(isinstance(row[0], float) for row in body["rows"])


def test_paging_is_stable_across_offsets(client):
    """A total order, or a row shows up on two pages and another on none."""
    first = client.get("/api/data/observations/rows?limit=5&offset=0").json()["rows"]
    second = client.get("/api/data/observations/rows?limit=5&offset=5").json()["rows"]

    # Ten rows, five and five, and nothing seen twice.
    assert len(first) == len(second) == 5
    assert [r for r in first if r in second] == []


# -- aggregate ----------------------------------------------------------------


def test_aggregate_groups_and_sums(client):
    body = client.get(
        "/api/data/observations/aggregate?group_by=series_code&agg=sum&measure=value"
    ).json()

    totals = dict(body["rows"])
    assert totals["USA"] == pytest.approx(5.69e13)
    assert totals["DEU"] is None  # NULL preserved, not coerced to 0
    assert body["measure"] == "sum_value"


def test_aggregate_counts_without_a_measure(client):
    body = client.get("/api/data/observations/aggregate?group_by=series_code").json()
    assert dict(body["rows"])["USA"] == 2


def test_aggregate_inside_a_thing_groups_only_its_rows(client, usa):
    """The useful case: the yearly total *of one series*, not of the whole lake. The
    thing's own filters are applied before the grouping, so the other eight rows are not
    in any bucket."""
    body = client.get(f"/api/data/{usa}/aggregate?group_by=year&agg=sum&measure=value").json()

    assert body["id"] == usa
    # The USA's two years, and nothing else's.
    assert {year for year, _ in body["rows"]} == {2023, 2024}


def test_aggregate_applies_the_filters_before_grouping(client):
    body = client.get("/api/data/observations/aggregate?group_by=series_code&year=2024").json()
    counts = dict(body["rows"])

    # 2023 is filtered out first, so each country is counted once rather than twice.
    assert counts["USA"] == 1
    assert counts["IDN"] == 1
    # SEKI writes no series_code, so its five 2024 rows group under NULL — a bucket of
    # its own, not a silent disappearance.
    assert counts[None] == 5


# -- attacks ------------------------------------------------------------------
#
# There is no SQL endpoint left to attack, so the old write and filesystem probes have
# nowhere to enter. What replaces them is the question that matters for this surface:
# a caller controls filter *values*, and names columns, tables, sorts and aggregates.
# Values are bound; names are looked up in the catalog and replaced by the catalog's own
# copy. Nothing a caller sends is ever written into the SQL text — these assert that.

INJECTIONS = [
    # The classic: close the string, add a statement.
    "x'; DROP TABLE lake.observations; --",
    "' OR 1=1 --",
    "'; UPDATE lake.observations SET value = 0; --",
    # A filesystem read, if the value were interpolated into a FROM.
    "x') UNION SELECT * FROM read_csv('/etc/passwd') --",
    '" OR "1"="1',
]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_an_injected_filter_value_is_matched_literally(client, payload):
    """It is a *value*, so it finds nothing. It does not become SQL."""
    r = client.get("/api/data/observations/rows", params={"series_code": payload})

    assert r.status_code == 200
    body = r.json()
    assert body["rows"] == []
    assert body["total"] == 0


@pytest.mark.parametrize("payload", INJECTIONS)
def test_the_table_survives_every_injected_value(client, payload):
    client.get("/api/data/observations/rows", params={"series_code": payload})
    client.get(
        "/api/data/observations/aggregate",
        params={"group_by": "series_code", "series_code": payload},
    )
    assert client.get("/api/tables/observations").json()["row_count"] == 10


@pytest.mark.parametrize("field", ["select", "sort", "group_by"])
def test_an_injected_identifier_is_rejected_not_interpolated(client, field):
    """A column name is looked up, not quoted. One that is not in the catalog raises."""
    params = {field: 'value" FROM lake.observations; DROP TABLE lake.observations --'}
    if field == "group_by":
        r = client.get("/api/data/observations/aggregate", params=params)
    else:
        r = client.get("/api/data/observations/rows", params=params)

    assert r.status_code == 422
    assert "unknown column" in r.json()["detail"]
    assert client.get("/api/tables/observations").json()["row_count"] == 10


def test_an_injected_aggregate_function_is_rejected(client):
    """`agg` is the one caller string that reaches the SQL text, so it is an allowlist."""
    injected = "count(*) FROM lake.observations; DROP TABLE x --"
    r = client.get(
        "/api/data/observations/aggregate",
        params={"group_by": "series_code", "agg": injected},
    )
    assert r.status_code == 422
    assert "unknown aggregate" in r.json()["detail"]


@pytest.mark.parametrize("payload", INJECTIONS)
def test_an_injected_id_is_a_404_not_an_injection_point(client, payload):
    """An id is looked up in the catalog, never interpolated. One that names nothing
    raises at `resolve` and never reaches the query — which is exactly what makes it
    safe to put a caller's string in a path segment."""
    r = client.get(f"/api/data/{payload}/rows")

    assert r.status_code == 404
    assert client.get("/api/tables/observations").json()["row_count"] == 10


def test_a_filter_naming_no_column_is_rejected_rather_than_ignored(client):
    """A filter that silently did nothing would hand back the whole table to someone
    who asked for part of it, and they would have no way to tell."""
    r = client.get("/api/data/observations/rows?nonexistent=1")

    assert r.status_code == 422
    assert "unknown column" in r.json()["detail"]


def test_a_wildcard_in_a_search_is_matched_literally(client):
    """Someone searching for `%` means the character, not "everything"."""
    body = client.get("/api/data/observations/rows?series_code=contains:%").json()
    assert body["total"] == 0


# -- rate limiting ------------------------------------------------------------


def test_query_tier_returns_429_after_the_limit(client):
    """The 31st row read in a window is throttled; health is never limited."""
    # default query limit is 30/min. Exhaust it.
    codes = []
    for _ in range(32):
        r = client.get("/api/data/observations/rows?limit=1")
        codes.append(r.status_code)

    assert codes.count(200) == 30
    assert 429 in codes
    # the throttled response tells the client when to retry
    r = client.get("/api/data/observations/rows?limit=1")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) >= 1


def test_describing_a_table_is_not_the_query_tier(client):
    """The catalog is cheap and the rows are not. Reading a schema 40 times is fine."""
    for _ in range(40):
        assert client.get("/api/tables/observations").status_code == 200


def test_health_is_never_rate_limited(client):
    for _ in range(200):
        assert client.get("/api/health").status_code == 200
