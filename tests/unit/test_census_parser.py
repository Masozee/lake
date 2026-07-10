"""The streaming CSV parser. Takes a path, not bytes — the file is gigabytes."""

from __future__ import annotations

import gzip

import pytest

from lake.sources.census_full.parser import (
    clean,
    count_rows,
    iter_records,
    normalise_key,
    peek,
)

CSV = (
    "Region Name,Total Population ,Median-Age,Notes\n"
    "Java,151000000,29.7,\n"
    "Sumatra,59000000,N/A,partial\n"
    "Papua,-999,31.2,-\n"
)


@pytest.fixture
def dump(tmp_path):
    path = tmp_path / "census_2026_000.csv.gz"
    path.write_bytes(gzip.compress(CSV.encode()))
    return path


def test_keys_are_normalised():
    assert normalise_key("Total Population ") == "total_population"
    assert normalise_key("Median-Age") == "median_age"


@pytest.mark.parametrize("sentinel", ["", "NA", "N/A", "null", "-", "-999", "  "])
def test_null_sentinels_become_none(sentinel):
    """A missing value is not zero. Coercing it is how a dataset goes quietly wrong."""
    assert clean(sentinel) is None


def test_real_values_survive():
    assert clean(" 29.7 ") == "29.7"
    assert clean("0") == "0"  # a genuine zero is not a null


def test_iter_records_yields_clean_dicts(dump):
    records = list(iter_records(dump))
    assert len(records) == 3
    assert records[0] == {
        "region_name": "Java",
        "total_population": "151000000",
        "median_age": "29.7",
        "notes": None,
    }
    assert records[1]["median_age"] is None  # was "N/A"
    assert records[2]["total_population"] is None  # was "-999"
    assert records[2]["notes"] is None  # was "-"


def test_count_rows_excludes_the_header(dump):
    assert count_rows(dump) == 3


def test_peek_does_not_read_the_whole_file(dump):
    assert len(peek(dump, 2)) == 2


def test_iter_records_is_lazy(dump):
    """A generator, not a list. On a 4 GB dump the difference is an OOM kill."""
    import types

    assert isinstance(iter_records(dump), types.GeneratorType)
