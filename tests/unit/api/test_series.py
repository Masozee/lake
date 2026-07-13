"""The three rungs, addressed by id.

A source publishes a dataset; a dataset is made of groups; a group is made of series.
That shape is the same for every source, and holding it that way is the whole point:
SEKI's 108 statistical tables and the World Bank's one indicator are the same kind of
thing — a group — even though Bank Indonesia numbers its tables (`I.1.`) and the World
Bank codes its indicators (`NY.GDP.MKTP.CD`).

The hard part is that a series name does not identify a series. Twenty-three of SEKI's
are called "Lainnya" ("Other") and mean something different in each table, so a series
is only ever *identified* by the triple `(dataset, group, series)` — which is exactly
what an id is a hash of.

The fixture reproduces the things that bite: a name reused across groups, a name
containing a colon, a `row_no` that decides which series is a group's headline, and
two publishers keying their groups completely differently.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration

SEKI = "seki_indicators"
GDP = "gdp_annual"

#: The World Bank's own key for the one indicator `gdp_annual` publishes. Its group.
GDP_GROUP = "NY.GDP.MKTP.CD"


@pytest.fixture
def catalog(replica):
    from lake.api import catalog as module

    return module


@pytest.fixture
def ids(catalog):
    """The ids of the fixture's things, by the keys behind them.

    Derived, never hard-coded: an id is a hash of the keys, so writing `wm72qlsa`
    into a test would assert the hash function rather than the behaviour.
    """
    return {
        "seki": catalog.id_for(SEKI),
        "uang": catalog.id_for(SEKI, "I.1."),
        "suku": catalog.id_for(SEKI, "I.2."),
        "m2": catalog.id_for(SEKI, "I.1.", "M2"),
        "lainnya_1": catalog.id_for(SEKI, "I.1.", "Lainnya"),
        "lainnya_2": catalog.id_for(SEKI, "I.2.", "Lainnya"),
        "colon": catalog.id_for(SEKI, "I.1.", "Aset:"),
        "gdp": catalog.id_for(GDP),
        "gdp_group": catalog.id_for(GDP, GDP_GROUP),
        "gdp_idn": catalog.id_for(GDP, GDP_GROUP, "Indonesia"),
    }


# --- the shape ---------------------------------------------------------------


def test_every_source_fills_the_same_middle_rung(catalog):
    """The point of the whole schema. Bank Indonesia numbers its tables and the World
    Bank codes its indicators — two completely different keying schemes — and both go
    in the same column, and neither is ever NULL.

    When `group_id` was allowed to be NULL, every consumer below carried a branch for
    "a source with no groups", and a shape that is only sometimes there is a shape
    every caller gets to be wrong about."""
    groups = catalog.groups_of()
    by_dataset = {g["dataset_id"]: g for g in groups}

    assert by_dataset[GDP]["group_id"] == GDP_GROUP
    assert by_dataset[GDP]["title"] == "GDP (current US$)"
    assert {g["group_id"] for g in groups if g["dataset_id"] == SEKI} == {"I.1.", "I.2."}

    # Never NULL. Not for any source, at any rung.
    assert all(g["group_id"] for g in groups)
    assert all(s["group_id"] for s in catalog.series_of())


def test_a_group_key_is_the_publishers_own(catalog, ids):
    """`I.1.` is what Bank Indonesia calls the table in its own publication. It is NOT
    `TABEL1_1`, which is what they happen to call the spreadsheet on their web server
    — a key that would move the day they reorganised it."""
    uang = catalog.resolve(ids["uang"])

    assert uang.group_id == "I.1."
    assert uang.title == "Uang Beredar"


# --- the id ------------------------------------------------------------------


def test_an_id_is_short_and_unambiguous(catalog, ids):
    """Eight characters of base32 — no `0`/`O`, no `1`/`l`, nothing to escape in a
    URL. Which is what lets the keys underneath stay ugly and honest: `I.1.` has dots
    in it and a series name has parentheses, and neither ever reaches a browser."""
    for value in ids.values():
        assert len(value) == 8
        assert value.isalnum() and value.islower()


def test_an_id_resolves_to_the_keys_the_database_holds(catalog, ids):
    seki = catalog.resolve(ids["seki"])
    assert (seki.dataset_id, seki.group_id, seki.series) == (SEKI, None, None)
    assert seki.level == "dataset"

    uang = catalog.resolve(ids["uang"])
    assert (uang.dataset_id, uang.group_id, uang.series) == (SEKI, "I.1.", None)
    assert uang.level == "group"

    m2 = catalog.resolve(ids["m2"])
    assert (m2.dataset_id, m2.group_id, m2.series) == (SEKI, "I.1.", "M2")
    assert m2.level == "series"

    # The World Bank's rungs are the same rungs, and its country sits under its
    # indicator exactly as SEKI's M2 sits under its table.
    idn = catalog.resolve(ids["gdp_idn"])
    assert (idn.dataset_id, idn.group_id, idn.series) == (GDP, GDP_GROUP, "Indonesia")


def test_an_id_is_the_same_after_a_rebuild(catalog, ids):
    """It is a hash of the keys, not a row in a table. Delete the replica, rebuild it,
    and every link anyone shared still resolves — with no migration and nothing to
    keep in sync."""
    assert catalog.id_for(SEKI, "I.1.", "M2") == ids["m2"]
    assert catalog.id_for(SEKI, "I.1.", "M2") == catalog.id_for(SEKI, "I.1.", "M2")


def test_two_series_with_the_same_name_get_different_ids(catalog, ids):
    """The point of hashing the triple rather than the name. Both are "Lainnya"; they
    are different series in different groups, and one URL cannot address both."""
    assert ids["lainnya_1"] != ids["lainnya_2"]
    assert catalog.resolve(ids["lainnya_1"]).group_id == "I.1."
    assert catalog.resolve(ids["lainnya_2"]).group_id == "I.2."


def test_every_id_in_the_lake_is_unique(catalog):
    """The resolver would raise on a collision rather than serve the wrong series.
    This is the assertion that it never has to."""
    things = catalog.id_map()
    keys = {(t.dataset_id, t.group_id, t.series) for t in things.values()}

    assert len(keys) == len(things)


def test_an_id_naming_nothing_raises(catalog):
    """A 404, not an empty page dressed up as real data. This is also the injection
    defence: an id is looked up, never interpolated."""
    for bad in ("zzzzzzzz", "", "'; DROP TABLE x--"):
        with pytest.raises(KeyError):
            catalog.resolve(bad)


def test_a_series_name_with_a_colon_survives(catalog, ids):
    """The fixture's `Aset:` ends in a colon — seven of SEKI's real series names do.
    An id has no parts, so there is nothing for a punctuation mark to break."""
    assert catalog.resolve(ids["colon"]).series == "Aset:"
    assert catalog.describe_dataset(ids["colon"])["title"] == "Aset:"


def test_the_trail_back_up_is_carried_by_the_page(catalog, ids):
    """What an id costs. `wm72qlsa` tells a reader nothing about where they are, so
    every detail page carries the titles above it."""
    crumbs = catalog.describe_dataset(ids["m2"])["crumbs"]

    assert [c["title"] for c in crumbs] == [SEKI, "Uang Beredar", "M2"]
    assert [c["id"] for c in crumbs] == [ids["seki"], ids["uang"], ids["m2"]]

    # Three rungs for the World Bank too — not two. There is no shortcut from the
    # dataset to a country any more.
    assert [c["title"] for c in catalog.describe_dataset(ids["gdp_idn"])["crumbs"]] == [
        GDP,
        "GDP (current US$)",
        "Indonesia",
    ]

    # A dataset is the top rung; there is nothing above it.
    assert [c["title"] for c in catalog.describe_dataset(ids["seki"])["crumbs"]] == [SEKI]


# --- the cards ---------------------------------------------------------------


def test_every_series_gets_its_own_card(catalog):
    cards = catalog.dataset_cards([])
    series = [c for c in cards if c["series"]]

    # Seven: SEKI's four (M2, Lainnya, Aset: in I.1.; Lainnya in I.2.) AND the World
    # Bank's three countries. Once a country IS a series, they are the same thing.
    assert len(series) == 7
    assert {c["title"] for c in series} == {
        "M2",
        "Lainnya",
        "Aset:",
        "Indonesia",
        "United States",
        "Germany",
    }


def test_a_world_bank_country_is_a_series_inside_a_group(catalog, ids):
    """The merge's one real decision, now carried all the way through: a country is
    what a GDP series is a series OF, and the indicator it belongs to is its group."""
    cards = {c["id"]: c for c in catalog.dataset_cards([])}

    idn = cards[ids["gdp_idn"]]
    assert idn["series"] == "Indonesia"
    assert idn["unit"] == "USD"
    assert idn["freq"] == "annual"
    # Its parent is the indicator, not the dataset. The middle rung is always there.
    assert idn["group_id"] == GDP_GROUP
    assert idn["parent_id"] == ids["gdp_group"]
    assert idn["parent_title"] == "GDP (current US$)"


def test_all_three_rungs_get_a_card(catalog, ids):
    cards = {c["id"]: c for c in catalog.dataset_cards([])}

    assert cards[ids["seki"]]["group_id"] is None  # the dataset
    assert cards[ids["uang"]]["group_id"] == "I.1."  # a group inside it
    assert cards[ids["m2"]]["series"] == "M2"  # a series inside that
    assert cards[ids["gdp_group"]]["title"] == "GDP (current US$)"  # and the same
    assert cards[ids["gdp_idn"]]["series"] == "Indonesia"  # shape for the World Bank


def test_a_series_carries_the_group_it_came_from(catalog, ids):
    """Its own name does not identify it. Two series here are called "Lainnya" and the
    parent is the only thing telling them apart on screen."""
    cards = {c["id"]: c for c in catalog.dataset_cards([])}

    assert cards[ids["lainnya_1"]]["title"] == cards[ids["lainnya_2"]]["title"] == "Lainnya"
    assert cards[ids["lainnya_1"]]["parent_title"] == "Uang Beredar"
    assert cards[ids["lainnya_2"]]["parent_title"] == "Suku Bunga"
    assert cards[ids["lainnya_1"]]["parent_id"] == ids["uang"]


def test_a_series_sorts_under_its_own_group_in_publisher_order(catalog):
    """A series is a row *of* a group. Alphabetising it across the whole catalogue
    would scatter one table's rows among thousands of unrelated ones."""
    cards = [c for c in catalog.dataset_cards([]) if c["dataset_id"] == SEKI]
    order = [(c["group_id"], c["series"]) for c in cards]

    # The dataset card, then I.1.'s group, then its series in row_no order, then I.2.
    assert order[0] == (None, None)
    assert order[1] == ("I.1.", None)
    assert order[2:5] == [("I.1.", "M2"), ("I.1.", "Lainnya"), ("I.1.", "Aset:")]
    assert order[5] == ("I.2.", None)


def test_a_group_is_labelled_as_one(catalog, ids):
    cards = {c["id"]: c for c in catalog.dataset_cards([])}
    assert "series" in cards[ids["m2"]]["labels"]
    assert "group" in cards[ids["uang"]]["labels"]
    assert "group" in cards[ids["gdp_group"]]["labels"]
    assert "dataset" in cards[ids["seki"]]["labels"]


# --- what is inside one ------------------------------------------------------


def test_a_dataset_lists_its_groups(catalog, ids):
    """Without this a detail page is a dead end: a reader can see that SEKI has two
    groups and has no way to open one."""
    children = catalog.children_of(ids["seki"])

    assert children["level"] == "group"
    assert children["total"] == 2
    assert [c["title"] for c in children["items"]] == ["Uang Beredar", "Suku Bunga"]
    assert children["items"][0]["id"] == ids["uang"]


def test_a_group_lists_its_series_in_publisher_order(catalog, ids):
    """Row 1 of "Uang Beredar" is M2, which is what the table is named for."""
    children = catalog.children_of(ids["uang"])

    assert children["level"] == "series"
    assert [c["title"] for c in children["items"]] == ["M2", "Lainnya", "Aset:"]


def test_the_world_bank_drills_down_the_same_way(catalog, ids):
    """One rule, no special case. A dataset's children are its groups; a group's are
    its series — and that is as true of an indicator with 260 countries in it as of a
    statistical table with 59 series."""
    groups = catalog.children_of(ids["gdp"])
    assert groups["level"] == "group"
    assert [c["title"] for c in groups["items"]] == ["GDP (current US$)"]

    countries = catalog.children_of(ids["gdp_group"])
    assert countries["level"] == "series"
    assert countries["total"] == 3
    assert {c["title"] for c in countries["items"]} == {"Indonesia", "United States", "Germany"}


def test_a_series_has_nothing_inside_it(catalog, ids):
    """The bottom rung. Opening one shows its rows, not another list."""
    assert catalog.children_of(ids["m2"]) == {"level": None, "items": [], "total": 0}


# --- what sits beside one ----------------------------------------------------


def test_a_series_has_siblings_even_though_it_has_no_children(catalog, ids):
    """Which is the point of them. A series is a cul-de-sac otherwise: the reader who
    wants the next of 59 has to navigate back to a list to get there."""
    siblings = catalog.siblings_of(ids["m2"])

    assert siblings["level"] == "series"
    assert [s["title"] for s in siblings["items"]] == ["M2", "Lainnya", "Aset:"]
    # Itself included: the list is a place, and it should show where you are standing.
    assert ids["m2"] in {s["id"] for s in siblings["items"]}


def test_siblings_are_the_parents_children(catalog, ids):
    """Asked from one rung down. The same query, not a second implementation that could
    quietly disagree with the first."""
    assert catalog.siblings_of(ids["m2"]) == catalog.children_of(ids["uang"])
    assert catalog.siblings_of(ids["uang"]) == catalog.children_of(ids["seki"])


def test_a_dataset_has_no_siblings(catalog, ids):
    """The top rung: nothing above it, so nothing beside it. An empty list rather than
    a crash — and rather than every other dataset in the lake, which is a different
    page's job."""
    assert catalog.siblings_of(ids["seki"]) == {"level": None, "items": [], "total": 0}


# --- filtering ---------------------------------------------------------------


def test_level_narrows_to_one_rung(catalog, ids):
    """Three rungs below a source, and every card is on exactly one of them."""
    cards = catalog.dataset_cards([])

    datasets = catalog.filter_cards(cards, level="dataset")
    groups = catalog.filter_cards(cards, level="group")
    series = catalog.filter_cards(cards, level="series")

    assert all(not c["group_id"] and not c["series"] for c in datasets)
    assert all(c["group_id"] and not c["series"] for c in groups)
    assert all(c["series"] for c in series)

    # Every card lands on one rung and no card lands on two.
    assert len(datasets) + len(groups) + len(series) == len(cards)
    assert {c["id"] for c in datasets} == {ids["gdp"], ids["seki"]}
    assert {c["id"] for c in groups} == {ids["uang"], ids["suku"], ids["gdp_group"]}


def test_a_source_that_has_collected_nothing_is_not_a_dataset(catalog):
    """It still gets a card — the page should say what is being gathered — but it has
    no id to open, and counting it as a dataset would claim we serve data we do not
    have."""
    sources = [
        {"source_id": "brand_new", "display_name": "Not collected yet", "enabled": True},
    ]
    cards = catalog.dataset_cards(sources)

    pending = next(c for c in cards if c["source_id"] == "brand_new")
    assert pending["queryable"] is False
    assert pending["id"] is None

    assert pending not in catalog.filter_cards(cards, level="dataset")


def test_searching_a_group_name_finds_its_series_too(catalog, ids):
    """Someone searching "uang beredar" wants that group AND the series inside it."""
    hits = catalog.filter_cards(catalog.dataset_cards([]), q="uang beredar")
    found = {c["id"] for c in hits}

    assert ids["uang"] in found  # the group
    assert ids["m2"] in found  # and a series inside it


def test_searching_the_publishers_number_finds_the_group(catalog, ids):
    """Someone with the SEKI publication open searches by the number printed in it."""
    hits = catalog.filter_cards(catalog.dataset_cards([]), q="I.2.")

    assert {c["id"] for c in hits} == {ids["suku"], ids["lainnya_2"]}


def test_searching_a_shared_series_name_finds_both(catalog, ids):
    """And the parent is what lets the reader tell them apart."""
    hits = catalog.filter_cards(catalog.dataset_cards([]), q="lainnya")

    assert {c["id"] for c in hits} == {ids["lainnya_1"], ids["lainnya_2"]}
    assert {c["parent_title"] for c in hits} == {"Uang Beredar", "Suku Bunga"}


# --- the detail page ---------------------------------------------------------


def test_a_series_describes_itself_and_its_parent(catalog, ids):
    d = catalog.describe_dataset(ids["m2"])

    assert d["title"] == "M2"
    assert d["level"] == "series"
    assert d["series"] == "M2"
    assert d["group_id"] == "I.1."
    assert d["parent_title"] == "Uang Beredar"
    assert d["parent_id"] == ids["uang"]
    assert d["row_count"] == 2  # M2 has two observations in the fixture
    assert d["unit"] == "Miliar Rp"
    # A series IS one series; reporting "1 series" on its own page is noise.
    assert d["series_count"] is None


def test_a_dataset_counts_what_is_inside_it(catalog, ids):
    d = catalog.describe_dataset(ids["seki"])

    assert d["level"] == "dataset"
    assert d["group_count"] == 2
    assert d["series_count"] == 4

    # The World Bank has one group, and says so — where before it had none and the
    # page had to pretend the rung did not exist.
    gdp = catalog.describe_dataset(ids["gdp"])
    assert gdp["group_count"] == 1
    assert gdp["series_count"] == 3


def test_a_page_offers_its_own_id_rather_than_the_keys_behind_it(catalog, ids):
    """The whole argument for an id. `/api/data/{id}/rows` says everything that
    `?dataset_id=seki_indicators&group_id=I.1.&series=M2` says, in something a reader can
    hold in their head and paste into a paper."""
    query = catalog.describe_dataset(ids["m2"])["query"]

    assert query["id"] == ids["m2"]
    # No keys in the request. They are what the id stands in for.
    assert query["filters"] == {}


def test_an_id_expands_back_to_every_key_behind_it(catalog, ids):
    """`dataset_id` is never optional — series names are not unique across datasets — so
    all three keys have to come back out of the id, or the read is too wide."""
    from lake.api import rows

    table, pins = rows.pinned(ids["m2"])

    assert table == "observations"
    assert {(p["column"], p["value"]) for p in pins} == {
        ("dataset_id", "seki_indicators"),
        ("group_id", "I.1."),
        ("series", "M2"),
    }


def test_the_query_shown_on_a_page_actually_returns_rows(catalog, ids):
    """A request offered to a reader that the API rejects, or that comes back empty, is
    worse than no request at all. So run each one through the real read path."""
    from lake.api import rows

    for key in ("gdp", "gdp_group", "gdp_idn", "seki", "uang", "m2"):
        query = catalog.describe_dataset(ids[key])["query"]
        result = rows.rows(
            query["id"],
            select=query["select"],
            sort=query["sort"],
            descending=query["descending"],
            limit=5,
        )
        assert result["row_count"] > 0, key


def test_a_world_bank_id_expands_to_all_three_keys(catalog, ids):
    """Where it used to have two. Every rung is the same shape for every source now, so
    there is no source whose read is a term short."""
    from lake.api import rows

    _, pins = rows.pinned(ids["gdp_idn"])

    assert {(p["column"], p["value"]) for p in pins} == {
        ("dataset_id", "gdp_annual"),
        ("group_id", GDP_GROUP),
        ("series", "Indonesia"),
    }


def test_an_unknown_id_is_a_keyerror_not_an_empty_page(catalog):
    """An id naming nothing must 404, not render as a real dataset with no rows."""
    with pytest.raises(KeyError):
        catalog.describe_dataset("aaaaaaaa")


def test_a_series_chart_plots_that_series(catalog, ids):
    points = catalog.dataset_series(ids["m2"])

    assert [p["value"] for p in points] == [100.0, 110.0]


def test_a_group_chart_plots_its_headline_series(catalog, ids):
    """The first indicator the publisher lists is the one the table is named for, so
    it is the honest thing to plot — row_no 1, which is M2 here."""
    points = catalog.dataset_series(ids["uang"])

    assert [p["value"] for p in points] == [100.0, 110.0]  # M2, not Lainnya


def test_a_series_sample_drops_the_indicator_column(catalog, ids):
    """Every row would carry the same value — noise the reader already knows from the
    page title."""
    sample = catalog.dataset_sample(ids["m2"])

    assert sample["columns"] == ["period", "value", "unit"]
    assert len(sample["rows"]) == 2


# --- browsing it in the admin panel ------------------------------------------


def test_the_admin_browser_opens_a_series(catalog, ids):
    """Every source lands in one table, so a dataset IS a filtered view of it and
    browsing one is the same query with the filter already applied."""
    from lake.api.admin import browse

    page = browse.browse(ids["m2"])

    assert page["total"] == 2  # the series' own rows, not the whole table's
    assert page["table"] == "observations"
    assert page["pinned"] == ["dataset_id", "group_id", "series"]


def test_the_admin_browser_pins_the_same_columns_for_every_source(catalog, ids):
    """A World Bank series pins three columns, exactly as a SEKI one does. It used to
    pin two, because it had no group — which meant the pinned set depended on which
    publisher you happened to be looking at."""
    from lake.api.admin import browse

    page = browse.browse(ids["gdp_idn"])

    assert page["total"] == 2  # Indonesia's two years
    assert page["pinned"] == ["dataset_id", "group_id", "series"]


def test_the_admin_browser_can_still_open_the_raw_table(replica):
    """`observations` itself is browsable, unfiltered — it is where an admin looks
    when they do not yet know which dataset they want. The one thing addressed by name
    rather than by id: it is not a dataset, it is what they are all views of."""
    from lake.api.admin import browse

    page = browse.browse("observations")

    assert page["total"] == 10  # 5 GDP + 5 SEKI
    assert page["pinned"] == []


def test_a_readers_filters_compose_with_the_pinned_ones(catalog, ids):
    from lake.api.admin import browse

    page = browse.browse(ids["m2"], filters=[{"column": "value", "op": "gt", "value": 105}])

    assert page["total"] == 1  # only the 110.0 observation


def test_sql_in_an_id_is_not_sql(replica):
    """An id is resolved against the real catalog, never interpolated — so an injected
    fragment names nothing, which is a 404 rather than an execution."""
    from lake.api.admin import browse

    with pytest.raises(KeyError):
        browse.browse("'; DROP TABLE x--")


def test_an_id_naming_nothing_raises_rather_than_showing_an_empty_grid(replica):
    """A stale id and a dataset with no rows look identical on screen, and only one of
    them is a 404."""
    from lake.api.admin import browse

    with pytest.raises(KeyError):
        browse.browse("zzzzzzzz")
