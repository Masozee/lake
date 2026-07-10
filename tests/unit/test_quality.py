"""Statistical gate. Publishing zero rows is a perfectly successful HTTP 200."""

from __future__ import annotations

from lake.transform.quality import check_null_rate, check_primary_key_unique, check_row_count_sane


def test_zero_rows_always_fails():
    assert not check_row_count_sane(0, [1000, 1010, 990, 1005])


def test_row_count_within_tolerance_passes():
    assert check_row_count_sane(1002, [1000, 1010, 990, 1005])


def test_row_count_collapse_is_caught():
    """The source published a header and nothing else. HTTP 200. No exception."""
    result = check_row_count_sane(12, [1000, 1010, 990, 1005])
    assert not result
    assert result.detail["z"] > 3


def test_insufficient_history_only_asserts_non_empty():
    """A three-point standard deviation is worse than no interval at all."""
    result = check_row_count_sane(5, [1000, 1010])
    assert result
    assert result.detail["reason"] == "insufficient history"


def test_constant_history_demands_an_exact_match():
    assert check_row_count_sane(500, [500, 500, 500, 500])
    assert not check_row_count_sane(501, [500, 500, 500, 500])


def test_null_rate_flags_a_column_that_went_empty():
    result = check_null_rate({"gdp_usd": 950, "country_iso3": 0}, total_rows=1000)
    assert not result
    assert result.detail["offenders"] == {"gdp_usd": 0.95}


def test_null_rate_passes_when_sparse_but_sane():
    assert check_null_rate({"gdp_usd": 100}, total_rows=1000)


def test_primary_key_uniqueness_detects_duplicates():
    result = check_primary_key_unique(1000, 998, ["country_iso3", "year"])
    assert not result
    assert result.detail["dupes"] == 2
