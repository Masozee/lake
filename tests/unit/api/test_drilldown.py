"""The admin Data browser's drill-down.

There are ~4,300 datasets, tables, and series between them, which is not a list a
person can scroll — so the browser walks the tree one rung at a time. This is the
code that decides what is *inside* what.

The rule is easy to get subtly wrong, and the way it is wrong is covered below: a
statistical table's series are not its dataset's children, they are grandchildren.
Ids make the second old trap impossible — there is no string prefix to accidentally
match — but the parent-child test still compares the resolved keys, never the ids.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

DATASET = "seki_indicators"

#: The World Bank's own key for the one indicator `gdp_annual` publishes.
GDP_GROUP = "NY.GDP.MKTP.CD"


@pytest.fixture
def signed_in(client):
    """The Data browser is behind the admin login like everything else.

    These unit tests run with no Postgres, so a real session is impossible. The
    principal is stubbed; the auth boundary itself — that every admin route 401s
    without a cookie — is covered in tests/integration/test_admin_routes.py.
    """
    import uuid

    from lake.api.admin.auth import Principal
    from lake.api.routes import admin as module

    who = Principal(uuid.uuid4(), "test@example.com", "Test")
    client.app.dependency_overrides[module.principal] = lambda: who
    yield client
    client.app.dependency_overrides.clear()


@pytest.fixture
def ids(replica):
    """The ids of the fixture's things, derived rather than written down.

    An id is a hash of the keys behind it, so hard-coding one into a test would
    assert the hash function rather than the behaviour.
    """
    from lake.api import catalog

    return {
        "seki": catalog.id_for(DATASET),
        "uang": catalog.id_for(DATASET, "I.1."),
        "suku": catalog.id_for(DATASET, "I.2."),
        "m2": catalog.id_for(DATASET, "I.1.", "M2"),
        "gdp": catalog.id_for("gdp_annual"),
        # The World Bank's group: the one indicator `gdp_annual` publishes, keyed the
        # way the World Bank keys it. Every source has groups now.
        "gdp_group": catalog.id_for("gdp_annual", GDP_GROUP),
    }


@pytest.fixture
def ask(signed_in):
    """Ask for one level of the tree."""

    def _ask(parent: str = "", **params) -> dict:
        r = signed_in.get("/api/admin/data", params={"parent": parent, **params})
        assert r.status_code == 200, r.text
        return r.json()

    return _ask


# --- the direct-child rule ---------------------------------------------------


def test_the_root_lists_the_raw_table_and_every_dataset(ask, ids):
    body = ask()

    by_level = {i["level"]: i for i in body["items"]}
    # The raw table is the one thing addressed by name: it is not a dataset, it is
    # what every dataset is a filtered view of.
    assert by_level["raw"]["id"] == "observations"
    assert {i["id"] for i in body["items"] if i["level"] == "dataset"} == {
        ids["gdp"],
        ids["seki"],
    }
    assert body["crumbs"] == []


def test_a_dataset_row_is_titled_by_what_its_source_is_called(ask, ids):
    """`gdp_annual` is our internal key. Nobody is looking for that — they are looking
    for the World Bank. So the row says what the source is CALLED, and keeps the key
    underneath for whoever is writing a query."""
    body = ask()
    row = next(i for i in body["items"] if i["id"] == ids["gdp"])

    assert row["title"] == "World Bank GDP indicator"
    assert row["dataset_id"] == "gdp_annual"
    assert row["source_id"] == "worldbank_gdp"
    assert row["kind"] == "api"
    assert row["schedule"] == "daily"
    # The one line on this page that tells a reader who does not already know what any
    # of this IS.
    assert "GDP in current US dollars" in row["description"]


def test_a_source_that_has_published_nothing_still_gets_a_row(ask):
    """It is exactly the row an admin opens this page to find. Leaving it out would
    mean the page can only ever show you what is already working.

    It has no id, so there is nothing to open — the row is a statement, not a link."""
    body = ask()
    pending = [i for i in body["items"] if i["level"] == "source"]

    assert pending, "the registry has sources that have collected nothing"
    for row in pending:
        assert row["id"] is None
        assert row["openable"] is False
        assert row["source_id"]
        assert row["description"]


def test_a_filter_matches_the_description_a_reader_can_see(ask):
    """The row shows a description; searching it must find it. Otherwise the page is
    showing you a fact it will not let you search on."""
    body = ask(q="consumer price inflation")

    assert [i["source_id"] for i in body["items"]] == ["bps_inflation"]


def test_a_source_row_says_whether_it_is_keeping_to_its_schedule(ask, monkeypatch):
    """The thing an admin opens this page to check. A paused source is NOT stale — it is
    off because someone turned it off, and calling that a fault would page a person at
    3am about a decision they made themselves."""
    from lake.api.admin import monitor

    monkeypatch.setattr(
        monitor,
        "freshness",
        lambda: [
            {
                "source_id": "worldbank_gdp",
                "display_name": "World Bank GDP indicator",
                "schedule": "daily",
                "enabled": True,
                "freshness_sla_hours": 30,
                "last_success_at": None,
                "last_status": None,
                "hours_since_success": 91.5,
                "is_stale": True,
            }
        ],
    )

    rows = {r["source_id"]: r for r in ask()["items"] if r.get("source_id")}

    late = rows["worldbank_gdp"]
    assert late["is_stale"] is True
    assert late["hours_since_success"] == 91.5
    assert late["sla_hours"] == 30

    # A source the view says nothing about carries no verdict either — the same rule as
    # a database that did not answer at all.
    assert "is_stale" not in rows["seki"]


def test_freshness_is_absent_rather_than_green_when_the_catalog_is_down(ask, monkeypatch):
    """The drill-down reads the serving replica alone and must keep answering when the
    catalog database is down — so a freshness lookup that fails costs the freshness
    column, not the page.

    And the column is ABSENT, not `is_stale: false`. "We could not check" and "it is
    fine" are not the same statement, and the second one is the lie that lets a dead
    source sit unnoticed on a page whose whole job is to notice."""
    from lake.api.admin import monitor

    def down() -> list:
        raise RuntimeError("connection refused")

    monkeypatch.setattr(monitor, "freshness", down)

    rows = ask()["items"]

    assert rows, "the page still answers"
    # Not one row claims to be fresh, or stale, or anything else about a database we
    # could not reach.
    assert not any("is_stale" in r for r in rows)
    # ...and everything the replica and the registry know is still there.
    assert any(r.get("description") for r in rows)


def test_a_dataset_lists_its_groups(ask, ids):
    body = ask(ids["seki"])

    assert {i["id"] for i in body["items"]} == {ids["uang"], ids["suku"]}
    assert all(i["level"] == "group" for i in body["items"])
    # Its groups' series are grandchildren, and must not appear here.
    assert not any(i["level"] == "series" for i in body["items"])


def test_every_source_drills_down_the_same_way(ask, ids):
    """One rule, no special case. The World Bank used to have no middle rung at all,
    so its countries hung straight off the dataset and this level did not exist."""
    body = ask(ids["gdp"])

    assert [i["level"] for i in body["items"]] == ["group"]
    assert [i["title"] for i in body["items"]] == ["GDP (current US$)"]

    countries = ask(ids["gdp_group"])
    assert all(i["level"] == "series" for i in countries["items"])
    assert {i["title"] for i in countries["items"]} == {"Indonesia", "United States", "Germany"}


def test_a_group_lists_its_series(ask, ids):
    body = ask(ids["uang"])

    assert all(i["level"] == "series" for i in body["items"])
    assert {i["title"] for i in body["items"]} == {"M2", "Lainnya", "Aset:"}


def test_a_series_has_nothing_inside_it(ask, ids):
    """The bottom rung. Opening one shows its rows, not another list."""
    body = ask(ids["m2"])

    assert body["items"] == []
    assert body["total"] == 0
    # ...but the crumbs still say where it is, because the id does not.
    assert [c["title"] for c in body["crumbs"]] == [DATASET, "Uang Beredar", "M2"]


def test_only_a_dataset_or_a_group_can_be_drilled_into(ask, ids):
    """`openable` is what the UI reads to decide whether a row gets a caret, so it must
    agree with the level. Three kinds of row have nothing below them, for three
    different reasons: a series is the bottom rung, the raw table is what all of them
    are views of, and a source that has published nothing has nothing to show."""
    for parent in ("", ids["seki"], ids["uang"], ids["gdp"], ids["gdp_group"]):
        for item in ask(parent)["items"]:
            expected = item["level"] in ("dataset", "group")
            assert item["openable"] is expected, item


def test_an_id_naming_nothing_is_a_404(signed_in):
    """Not an empty list. A stale id and an empty level look identical on screen,
    and only one of them is a mistake the reader should be told about."""
    r = signed_in.get("/api/admin/data", params={"parent": "zzzzzzzz"})

    assert r.status_code == 404


# --- the traps ---------------------------------------------------------------


def test_a_datasets_grandchildren_are_not_its_children(replica):
    """Comparing the resolved keys, never the ids: a group's series belong to the
    group, and pulling them up a level would put 3,918 things where 108 belong."""
    from lake.api.routes.admin import _is_direct_child

    parent = ("seki_indicators", None, None)

    assert _is_direct_child(("seki_indicators", "I.1.", None), parent)  # a group
    assert not _is_direct_child(("seki_indicators", "I.1.", "M2"), parent)  # its series


def test_a_groups_children_are_its_own_series(replica):
    from lake.api.routes.admin import _is_direct_child

    parent = ("seki_indicators", "I.1.", None)

    assert _is_direct_child(("seki_indicators", "I.1.", "M2"), parent)
    # A sibling group's series is not ours, whatever its key looks like.
    assert not _is_direct_child(("seki_indicators", "I.10.", "M2"), parent)


def test_another_datasets_children_are_not_ours(replica):
    from lake.api.routes.admin import _is_direct_child

    assert not _is_direct_child(
        ("gdp_annual", "NY.GDP.MKTP.CD", None), ("seki_indicators", None, None)
    )


# --- crumbs, filtering, paging -----------------------------------------------


def test_the_crumbs_name_the_trail_back_up(ask, ids):
    """The whole cost of an opaque id, paid here. `wm72qlsa` says nothing about where
    the reader is; these crumbs are the only thing that does."""
    crumbs = ask(ids["uang"])["crumbs"]

    assert [c["id"] for c in crumbs] == [ids["seki"], ids["uang"]]
    assert [c["title"] for c in crumbs] == [DATASET, "Uang Beredar"]


def test_the_filter_narrows_this_level_only(ask, ids):
    """Filtering SEKI's tables must not drag in the World Bank's countries."""
    body = ask(ids["uang"], q="lain")

    assert [i["title"] for i in body["items"]] == ["Lainnya"]


def test_a_level_pages_rather_than_dumping(ask, ids):
    """`NY.GDP.MKTP.CD` has 260 countries in production; a page of them is what the
    browser can render."""
    body = ask(ids["gdp_group"], size=2)

    assert len(body["items"]) == 2
    assert body["total"] == 3  # the fixture's three countries
    assert body["pages"] == 2

    second = ask(ids["gdp_group"], size=2, page=1)
    assert len(second["items"]) == 1
    # No row appears on two pages, and none is skipped.
    assert not {i["id"] for i in body["items"]} & {i["id"] for i in second["items"]}


# --- the detail page ---------------------------------------------------------


def test_a_things_detail_says_what_the_list_cannot(signed_in, ids):
    """The list can only give a title and a row count. This is what the id hides:
    where it came from, how far back it runs, how much of it is missing."""
    r = signed_in.get(f"/api/admin/data/{ids['uang']}/detail")

    assert r.status_code == 200, r.text
    body = r.json()

    assert body["title"] == "Uang Beredar"
    assert body["level"] == "group"
    assert body["series_count"] == 3
    assert body["group_id"] == "I.1."  # Bank Indonesia's own numbering
    assert body["source_id"] == "seki"
    assert [c["title"] for c in body["crumbs"]] == [DATASET, "Uang Beredar"]
    # The request that reads it: its own id. Not SQL, and not the keys behind the id —
    # `/api/data/{id}/rows` is what the page's snippets are built from.
    assert body["query"]["id"] == body["id"]
    assert body["query"]["filters"] == {}


def test_a_series_has_a_page_of_its_own(signed_in, ids):
    """A series is not a lesser thing than the group it sits in — it is what a reader
    came for. So it gets everything a page needs in one request: its facts, the line
    to chart, and the rows below."""
    body = signed_in.get(f"/api/admin/data/{ids['m2']}/detail").json()

    assert body["level"] == "series"
    assert [p["value"] for p in body["points"]] == [100.0, 110.0]
    # The bottom rung: nothing inside it, so the page shows rows rather than a list.
    assert body["children"]["items"] == []
    assert [c["title"] for c in body["crumbs"]] == [DATASET, "Uang Beredar", "M2"]


def test_a_series_page_lists_what_sits_beside_it(signed_in, ids):
    """Its children are empty — it is the bottom rung. Without its *siblings* the page
    is a cul-de-sac: a reader comparing M2 against the lines under it would have to
    navigate back to a list, find their place, and come back in, once per series."""
    body = signed_in.get(f"/api/admin/data/{ids['m2']}/detail").json()

    siblings = body["siblings"]
    assert siblings["level"] == "series"
    # The publisher's own order, and the thing itself is IN the list: the list is a
    # place, and a place a reader is standing in should show where they are standing.
    assert [s["title"] for s in siblings["items"]] == ["M2", "Lainnya", "Aset:"]
    assert ids["m2"] in {s["id"] for s in siblings["items"]}


def test_a_datasets_page_has_no_siblings_to_show(signed_in, ids):
    """A dataset is the top rung. There is nothing above it, so there is nothing
    beside it — and an empty list is the honest answer, not a crash."""
    body = signed_in.get(f"/api/admin/data/{ids['seki']}/detail").json()

    assert body["siblings"] == {"level": None, "items": [], "total": 0}


def test_a_page_carries_the_api_url_its_snippets_must_use(signed_in, ids):
    """The page shows copy-paste code for four languages, and that code has to name a
    URL the reader can actually reach. The browser cannot know it — the frontend talks
    to the API over a proxy — so the server says."""
    body = signed_in.get(f"/api/admin/data/{ids['m2']}/detail").json()

    assert body["api_url"].startswith("http")
    # No trailing slash: the page appends `/api/tables/…` to it.
    assert not body["api_url"].endswith("/")


def test_a_groups_page_charts_its_headline_series_and_lists_the_rest(signed_in, ids):
    """Above the series rung the honest line is the one the publisher lists first —
    row 1 of "Uang Beredar" is M2, which is what the table is named for."""
    body = signed_in.get(f"/api/admin/data/{ids['uang']}/detail").json()

    assert [p["value"] for p in body["points"]] == [100.0, 110.0]  # M2, not Lainnya
    assert [c["title"] for c in body["children"]["items"]] == ["M2", "Lainnya", "Aset:"]


def test_the_detail_of_an_unknown_id_is_a_404(signed_in):
    r = signed_in.get("/api/admin/data/zzzzzzzz/detail")

    assert r.status_code == 404
