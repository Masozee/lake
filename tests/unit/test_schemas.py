"""Record contracts. A row that fails here is quarantined, never published."""

from __future__ import annotations

from datetime import date

import pytest

from lake.sources.bps_inflation.schema import InflationRecord


def test_a_valid_record_passes():
    r = InflationRecord(region="Java", period=date(2026, 7, 1), inflation_pct=2.3)
    assert r.inflation_pct == 2.3


def test_period_must_be_the_first_of_the_month():
    """logical_date is the month the data describes, normalised to day 1."""
    with pytest.raises(ValueError, match="first of the month"):
        InflationRecord(region="Java", period=date(2026, 7, 9), inflation_pct=2.3)


@pytest.mark.parametrize("pct", [-60.0, 150.0])
def test_implausible_inflation_is_a_parsing_error(pct):
    """A figure this far out is a column offset or a raw index, not an economy."""
    with pytest.raises(ValueError):
        InflationRecord(region="Java", period=date(2026, 7, 1), inflation_pct=pct)


def test_extreme_but_possible_inflation_is_allowed():
    InflationRecord(region="Java", period=date(2026, 7, 1), inflation_pct=-49.0)
    InflationRecord(region="Java", period=date(2026, 7, 1), inflation_pct=99.0)


@pytest.mark.parametrize("label", ["Total", "JUMLAH", " nasional ", "Indonesia"])
def test_aggregate_rows_are_rejected(label):
    """Spreadsheets carry summary rows that look exactly like data rows."""
    with pytest.raises(ValueError, match="aggregate row"):
        InflationRecord(region=label, period=date(2026, 7, 1), inflation_pct=2.3)


def test_unknown_columns_are_rejected():
    """extra='forbid' is how you notice upstream added a column you ignored."""
    with pytest.raises(ValueError):
        InflationRecord(
            region="Java", period=date(2026, 7, 1), inflation_pct=2.3, new_column="surprise"
        )
