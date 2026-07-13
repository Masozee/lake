"""The JSON the frontend reads.

The UI in web/ renders whatever these return, so a wrong shape here is a broken
page there — and the page has no other source of truth. These check the shape and
the two rules the frontend depends on: an unknown id is a 404 (not an empty page
dressed up as real data), and the filter facets are computed from the whole set,
so narrowing by one never empties the others.
"""

from __future__ import annotations

import pytest

from lake.transform.runner import SCHEMA

pytestmark = pytest.mark.integration


@pytest.fixture
def gdp(replica):
    """The id of the `gdp_annual` dataset.

    Derived rather than written down: an id is a hash of the keys behind it, so a
    hard-coded one would assert the hash function rather than the behaviour.
    """
    from lake.api import catalog

    return catalog.id_for("gdp_annual")


def test_overview_carries_stats_sources_and_series(client):
    body = client.get("/api/ui/overview").json()

    # One table now: every source lands in the merged `observations`.
    assert body["stats"]["table_count"] == 1
    assert body["stats"]["total_rows"] == 10  # 5 World Bank rows + 5 SEKI
    assert body["stats"]["built_at"]  # the replica was just built
    assert [t["name"] for t in body["stats"]["tables"]] == ["observations"]
    assert isinstance(body["sources"], list)
    # The fixture's GDP rows have no 'WLD' aggregate, so there is no honest
    # headline series to draw. None, rather than a line through country rows.
    assert body["series"] is None


def test_stats_is_the_overview_without_the_chart(client):
    body = client.get("/api/ui/stats").json()

    assert set(body) == {"stats", "sources"}
    assert body["stats"]["total_rows"] == 10


def test_datasets_lists_one_card_per_dataset(client):
    body = client.get("/api/ui/datasets").json()

    # `total` counts the whole catalogue; `cards` is one page of it.
    assert body["total"] >= len(body["cards"])

    card = next(c for c in body["cards"] if c["dataset_id"] == "gdp_annual" and not c["group_id"])
    assert card["queryable"] is True
    assert card["row_count"] == 5  # the World Bank's five rows
    # Every card is a view of the one merged table, so they all report its width.
    assert card["column_count"] == len(SCHEMA)
    assert "queryable" in card["labels"]


def test_datasets_search_narrows_the_cards(client):
    all_cards = client.get("/api/ui/datasets").json()
    hit = client.get("/api/ui/datasets", params={"q": "gdp_annual"}).json()
    miss = client.get("/api/ui/datasets", params={"q": "no-such-dataset"}).json()

    assert len(hit["cards"]) >= 1
    assert miss["cards"] == []
    # `total` is the size of the unfiltered set: the page says "2 of 112", and
    # the second number must not move when you type.
    assert hit["total"] == miss["total"] == all_cards["total"]


def test_datasets_facets_come_from_the_unfiltered_set(client):
    """Narrowing by a facet must not delete the other options from the dropdown."""
    unfiltered = client.get("/api/ui/datasets").json()
    filtered = client.get("/api/ui/datasets", params={"status": "queryable"}).json()

    assert filtered["kinds"] == unfiltered["kinds"]
    assert filtered["sections"] == unfiltered["sections"]


def test_dataset_detail_carries_a_sample_and_the_query(client, gdp):
    body = client.get(f"/api/ui/dataset/{gdp}").json()

    assert body["dataset"]["id"] == gdp
    assert body["dataset"]["dataset_id"] == "gdp_annual"
    assert body["dataset"]["level"] == "dataset"
    assert body["dataset"]["row_count"] == 5
    # The API request that returns these rows — the page turns it into a link, a
    # download, and the copy-paste snippets. It is the thing's own id: there is no SQL
    # to hand anyone, and no keys to spell out either.
    assert body["dataset"]["query"]["id"] == gdp
    assert body["dataset"]["query"]["filters"] == {}
    # Above the series rung, `series` is what tells the rows apart, so it stays in
    # the sample. On a series page it would repeat the title on every row.
    assert body["sample"]["columns"] == ["period", "series", "value", "unit"]
    assert len(body["sample"]["rows"]) == 5
    # The World Bank gives no row order, so no series is its dataset's headline.
    assert body["series"] == []


def test_dataset_detail_lists_what_is_inside_it(client, gdp):
    """Without this the page is a dead end: a reader can see what a dataset holds and
    has no way to open any of it.

    A dataset's children are its groups — for every source. `gdp_annual` publishes one
    indicator, so it has one group, and that is a count rather than a special case."""
    body = client.get(f"/api/ui/dataset/{gdp}").json()

    assert body["dataset"]["group_count"] == 1
    assert body["children"]["level"] == "group"
    assert [c["title"] for c in body["children"]["items"]] == ["GDP (current US$)"]

    # And the countries are one rung further down, inside that indicator.
    group = body["children"]["items"][0]["id"]
    countries = client.get(f"/api/ui/dataset/{group}").json()["children"]

    assert countries["level"] == "series"
    assert countries["total"] == 3
    assert {c["title"] for c in countries["items"]} == {"Indonesia", "United States", "Germany"}


def test_dataset_detail_unknown_is_404(client):
    """A 404, not an empty page: an id that names nothing must not render as if it
    were a real dataset with no rows."""
    assert client.get("/api/ui/dataset/zzzzzzzz").status_code == 404


def test_table_detail_carries_schema_profile_and_sample(client):
    body = client.get("/api/ui/table/observations").json()

    assert body["table"]["name"] == "observations"
    assert body["table"]["row_count"] == 10  # 5 World Bank rows + 5 SEKI
    assert {c["name"] for c in body["table"]["columns"]} >= set(SCHEMA)
    assert "series" in body["profile"]
    assert len(body["sample"]["rows"]) == 10


def test_table_detail_unknown_is_404(client):
    assert client.get("/api/ui/table/secrets").status_code == 404


def test_contact_accepts_a_real_message(client):
    r = client.post(
        "/api/ui/contact",
        json={"name": "Ada", "email": "ada@example.com", "message": "Please add trade data."},
    )
    assert r.status_code == 200
    assert r.json() == {"ok": True, "name": "Ada"}


def test_contact_rejects_a_useless_one_and_says_why(client):
    """Every complaint is shown to the reader, so all of them must come back at
    once — not one per submit."""
    r = client.post("/api/ui/contact", json={"name": "", "email": "nope", "message": "hi"})

    assert r.status_code == 422
    errors = r.json()["detail"]["errors"]
    assert len(errors) == 3  # no name, bad email, too short


def test_contact_caps_a_hostile_payload(client):
    r = client.post(
        "/api/ui/contact",
        json={"name": "Ada", "email": "ada@example.com", "message": "x" * 5_000},
    )
    assert r.status_code == 422
    assert "too long" in r.json()["detail"]["errors"][0]
