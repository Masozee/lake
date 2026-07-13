"""The data browser's query builder.

This module writes SQL, so most of what follows is an attempt to make it write SQL
somebody else chose. It should not be possible: identifiers are resolved against
the real catalog and replaced with the catalog's own copy of the name, and every
value is a bound parameter rather than text spliced into a string.

The engine underneath is read-only with external access disabled, so even a
successful injection could not write or read a file. These tests are about the
layer that means one never gets that far.
"""

from __future__ import annotations

import pytest

pytestmark = pytest.mark.integration


@pytest.fixture
def browse(replica):
    """The module, against the fabricated gdp_annual replica from conftest."""
    from lake.api.admin import browse as module

    return module


@pytest.fixture
def gdp(replica):
    """The id of the `gdp_annual` dataset.

    Derived, never written down: an id is a hash of the keys behind it, so a
    hard-coded one would assert the hash function rather than the behaviour.
    """
    from lake.api import catalog

    return catalog.id_for("gdp_annual")


# --- what every row has in common --------------------------------------------


def test_a_view_says_what_never_varies_inside_it(browse, gdp, replica):
    """Browsing one series, four columns repeat the same string on every row —
    `dataset_id`, `group_id`, `group_title`, `series` — and push `period` and `value`,
    the two columns anyone came for, off the right edge of the screen.

    So the grid folds them away and the page states them once. Which columns those are
    is asked of the database, not assumed: `group_title` is constant because it is
    determined by `group_id`, and hard-coding that here would be a second copy of the
    schema, quietly rotting."""
    from lake.api import catalog

    m2 = catalog.id_for("seki_indicators", "I.1.", "M2")
    page = browse.browse(m2)

    assert page["constant"]["dataset_id"] == "seki_indicators"
    assert page["constant"]["group_id"] == "I.1."
    assert page["constant"]["group_title"] == "Uang Beredar"
    assert page["constant"]["series"] == "M2"

    # ...and `period` and `value` are not: they are what the reader came for.
    assert "period" not in page["constant"]
    assert "value" not in page["constant"]


def test_a_column_that_is_null_everywhere_is_constant_too(browse, replica):
    """SEKI gives no series codes, so `series_code` is NULL on every row. That is one
    value, and a column of 304 empty cells is exactly the noise this exists to fold."""
    from lake.api import catalog

    m2 = catalog.id_for("seki_indicators", "I.1.", "M2")
    page = browse.browse(m2)

    assert "series_code" in page["constant"]
    assert page["constant"]["series_code"] is None


def test_a_column_that_is_null_on_only_some_rows_is_not_constant(browse, gdp):
    """The trap. `count(DISTINCT value)` ignores NULLs, so a column holding one number
    and one gap counts ONE distinct value — and folding it away would hide the gap.

    The World Bank's five fixture rows carry four numbers and one NULL (Germany, whose
    figure is missing). Four distinct values, so the question does not arise there —
    but the guard is that a constant column must also be null-free."""
    page = browse.browse(gdp)

    assert "value" not in page["constant"]
    # `unit` really is constant: 'USD' on all five, no gaps.
    assert page["constant"]["unit"] == "USD"


def test_the_raw_table_folds_nothing_away(browse, replica):
    """`observations` unfiltered is where an admin looks when they do not yet know
    which dataset they want. Nothing about it is constant — that is the point of it —
    so every column stays in the grid."""
    page = browse.browse("observations")

    assert page["constant"] == {}
    assert page["pinned"] == []


# --- paging ------------------------------------------------------------------


def test_a_page_carries_its_own_pagination_facts(browse, gdp):
    page = browse.browse(gdp, size=2)

    assert page["total"] == 5  # the fixture's five rows
    assert page["size"] == 2
    assert page["pages"] == 3  # 5 rows / 2 per page, rounded up
    assert len(page["rows"]) == 2


def test_paging_walks_the_whole_table_without_repeating(browse, gdp):
    """Every row appears exactly once across the pages, and none is skipped."""
    seen = []
    for page in range(3):
        seen.extend(tuple(r) for r in browse.browse(gdp, page=page, size=2)["rows"])

    assert len(seen) == 5
    assert len(set(seen)) == 5


def test_a_page_past_the_end_is_empty_not_an_error(browse, gdp):
    assert browse.browse(gdp, page=99, size=25)["rows"] == []


def test_rows_come_back_newest_first_without_being_asked(browse, replica):
    """Parquet hands rows back in the order they were written, which for a time series
    is neither chronological nor stable. Page 1 of M2 used to open on February 2001,
    then September, then October — and a reader cannot tell whether that is the data or
    the display."""
    from lake.api import catalog

    m2 = catalog.id_for("seki_indicators", "I.1.", "M2")
    page = browse.browse(m2)

    column = next(i for i, c in enumerate(page["columns"]) if c["name"] == "period")
    periods = [row[column] for row in page["rows"]]

    assert periods == sorted(periods, reverse=True)


def test_the_default_sort_is_total_so_paging_cannot_repeat_a_row(browse, replica):
    """The subtle half of sorting. LIMIT/OFFSET slices an order, and rows that compare
    equal may come back in either order between one query and the next — so a
    non-total sort can show a row on both page 1 and page 2 while another appears on
    neither. The reader sees a duplicate and a hole and has no way to know the data is
    fine.

    The fixture's SEKI rows all share one period inside their group, so they are
    exactly the ties that break this."""
    from lake.api import catalog

    uang = catalog.id_for("seki_indicators", "I.1.")

    seen: list[tuple] = []
    for page in range(4):
        seen.extend(tuple(r) for r in browse.browse(uang, page=page, size=1)["rows"])

    assert len(seen) == 4  # the group's four observations
    assert len(set(seen)) == 4  # each exactly once: none repeated, none skipped


def test_the_page_size_is_capped(browse, gdp):
    """The browser renders every row it is handed. A 'page' of 10,000 is a hung tab."""
    assert browse.browse(gdp, size=10_000)["size"] == browse.MAX_PAGE_SIZE


# --- sorting -----------------------------------------------------------------


def test_sorting_is_done_by_the_database(browse, gdp):
    ascending = browse.browse(gdp, sort="year")
    descending = browse.browse(gdp, sort="year", descending=True)

    year = [c["name"] for c in ascending["columns"]].index("year")
    years = [r[year] for r in ascending["rows"]]

    assert years == sorted(years)
    assert [r[year] for r in descending["rows"]] == sorted(years, reverse=True)


def test_nulls_sort_last_in_both_directions(browse, gdp):
    """A column of mostly-nulls sorted descending should show the values, not a
    screen of empty cells."""
    for descending in (False, True):
        page = browse.browse(gdp, sort="value", descending=descending)
        column = [c["name"] for c in page["columns"]].index("value")
        values = [r[column] for r in page["rows"]]
        assert values[-1] is None  # DEU has a NULL gdp in the fixture
        assert values[0] is not None


# --- filtering ---------------------------------------------------------------


def test_contains_is_case_insensitive(browse, gdp):
    hit = browse.browse(gdp, filters=[{"column": "series", "op": "contains", "value": "indone"}])
    assert hit["total"] == 2  # both Indonesia rows


def test_total_counts_the_filtered_rows_not_the_table(browse, gdp):
    """ "Page 3 of 41" is a lie if the 41 counts rows the filter removed."""
    page = browse.browse(
        gdp,
        size=1,
        filters=[{"column": "series_code", "op": "equals", "value": "IDN"}],
    )
    assert page["total"] == 2
    assert page["pages"] == 2


def test_numeric_comparisons(browse, gdp):
    over = browse.browse(gdp, filters=[{"column": "year", "op": "gt", "value": 2023}])
    assert over["total"] == 3  # 2024 rows: IDN, USA, DEU


def test_null_filters(browse, gdp):
    empty = browse.browse(gdp, filters=[{"column": "value", "op": "empty"}])
    filled = browse.browse(gdp, filters=[{"column": "value", "op": "not_empty"}])

    assert empty["total"] == 1  # DEU
    assert filled["total"] == 4
    assert empty["total"] + filled["total"] == 5


def test_filters_combine_with_and(browse, gdp):
    page = browse.browse(
        gdp,
        filters=[
            {"column": "series_code", "op": "equals", "value": "IDN"},
            {"column": "year", "op": "gt", "value": 2023},
        ],
    )
    assert page["total"] == 1  # Indonesia, 2024


# --- injection ---------------------------------------------------------------


def test_an_unknown_id_is_refused(browse):
    """The id is resolved against the catalog. One that names nothing raises before
    any SQL is built."""
    with pytest.raises(KeyError):
        browse.browse("users")


def test_sql_in_an_id_is_not_sql(browse):
    """An id is looked up, never interpolated — so an injected fragment names no
    dataset, which is a 404 rather than an execution."""
    with pytest.raises(KeyError):
        browse.browse('gdp_annual"; DROP TABLE x; --')


def test_sql_in_a_sort_column_is_not_sql(browse, gdp):
    with pytest.raises(browse.BadFilter, match="unknown column"):
        browse.browse(gdp, sort="year DESC; DROP TABLE x")


def test_sql_in_a_filter_column_is_not_sql(browse, gdp):
    with pytest.raises(browse.BadFilter, match="unknown column"):
        browse.browse(
            gdp,
            filters=[{"column": 'year" OR 1=1 --', "op": "equals", "value": "x"}],
        )


def test_an_unknown_operator_is_refused(browse, gdp):
    """`op` is an allowlist, so it can never become a fragment of SQL."""
    with pytest.raises(browse.BadFilter, match="unknown operator"):
        browse.browse(gdp, filters=[{"column": "year", "op": "; DROP", "value": 1}])


def test_a_filter_value_is_bound_not_interpolated(browse, gdp):
    """The classic. If this returned all five rows, the value would be executing."""
    page = browse.browse(
        gdp,
        filters=[{"column": "series_code", "op": "equals", "value": "x' OR '1'='1"}],
    )
    assert page["total"] == 0  # matched literally, as a string that exists nowhere


def test_like_wildcards_in_a_search_value_are_literal(browse, gdp):
    """Someone searching for "%" means the character, not "match everything"."""
    page = browse.browse(gdp, filters=[{"column": "series", "op": "contains", "value": "%"}])
    assert page["total"] == 0


def test_an_underscore_in_a_search_value_is_literal(browse, gdp):
    """`_` is LIKE's single-character wildcard. Unescaped, "a_b" would match "axb"."""
    page = browse.browse(gdp, filters=[{"column": "series", "op": "contains", "value": "_"}])
    assert page["total"] == 0


# --- type sanity -------------------------------------------------------------


def test_comparing_text_with_gt_is_refused(browse, gdp):
    """Offering `>` on a country name could only ever produce a confusing error."""
    with pytest.raises(browse.BadFilter, match="cannot be compared"):
        browse.browse(gdp, filters=[{"column": "series", "op": "gt", "value": 5}])


def test_a_non_numeric_value_for_a_numeric_column_is_refused(browse, gdp):
    with pytest.raises(browse.BadFilter, match="not a number"):
        browse.browse(gdp, filters=[{"column": "year", "op": "gt", "value": "abc"}])


def test_values_come_back_json_safe(browse, gdp):
    """The frontend does arithmetic on these. A Decimal serialised as a string is
    the bug that keeps on giving."""
    page = browse.browse(gdp)
    for row in page["rows"]:
        for value in row:
            assert value is None or isinstance(value, bool | int | float | str)
