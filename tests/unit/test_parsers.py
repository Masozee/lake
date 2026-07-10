"""Parsers are pure functions, so they test in microseconds against real captured
bytes. That is the point: the day upstream renames a field, this suite goes red
instead of the lake quietly filling with nulls.

Refresh a fixture with the real response, never a hand-edited approximation:
    curl -s 'https://api.worldbank.org/...' > tests/fixtures/worldbank_gdp_page1.json
"""

from __future__ import annotations

from pathlib import Path

import pytest

from lake.sources.gov_news.parser import extract_pdf_links, parse_index
from lake.sources.worldbank_gdp.parser import parse as parse_gdp
from lake.sources.worldbank_gdp.schema import GdpRecord

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


# -- worldbank_gdp ------------------------------------------------------------


@pytest.fixture
def gdp_bytes() -> bytes:
    return (FIXTURES / "worldbank_gdp_page1.json").read_bytes()


def test_parse_extracts_country_records(gdp_bytes: bytes):
    records = parse_gdp(gdp_bytes)
    assert len(records) == 3  # the "World" aggregate has an empty iso3 and is dropped
    assert {r["country_iso3"] for r in records} == {"IDN", "USA"}


def test_parse_drops_aggregate_rows(gdp_bytes: bytes):
    assert all(r["country_iso3"] for r in parse_gdp(gdp_bytes))


def test_parse_preserves_null_values(gdp_bytes: bytes):
    """A missing GDP is real information. Do not coerce it to zero."""
    idn_2023 = next(r for r in parse_gdp(gdp_bytes) if r["year"] == 2023)
    assert idn_2023["gdp_usd"] is None


def test_parse_handles_empty_payload():
    assert parse_gdp(b'[{"page":1,"pages":1},null]') == []


def test_records_validate_against_the_schema(gdp_bytes: bytes):
    for raw in parse_gdp(gdp_bytes):
        record = GdpRecord.model_validate(raw)
        assert len(record.country_iso3) == 3


def test_schema_rejects_negative_gdp():
    with pytest.raises(ValueError, match="cannot be negative"):
        GdpRecord(country_iso3="IDN", year=2024, gdp_usd=-1.0)


def test_schema_rejects_bad_iso3():
    with pytest.raises(ValueError):
        GdpRecord(country_iso3="ID", year=2024, gdp_usd=1.0)


def test_schema_rejects_unknown_fields():
    """extra='forbid' is how you notice upstream added a column you ignored."""
    with pytest.raises(ValueError):
        GdpRecord(country_iso3="IDN", year=2024, gdp_usd=1.0, surprise="new field")


# -- gov_news -----------------------------------------------------------------


@pytest.fixture
def news_bytes() -> bytes:
    return (FIXTURES / "gov_news_index.html").read_bytes()


def test_extract_pdf_links_resolves_relative_urls(news_bytes: bytes):
    links = extract_pdf_links(news_bytes, "https://example.gov/news")
    assert "https://example.gov/files/outlook-2026-q2.pdf" in links
    assert "https://cdn.example.gov/population-2026.pdf" in links


def test_extract_pdf_links_deduplicates_and_keeps_order(news_bytes: bytes):
    links = extract_pdf_links(news_bytes, "https://example.gov/news")
    assert len(links) == len(set(links))
    assert links[0].endswith("outlook-2026-q2.pdf")


def test_extract_pdf_links_handles_query_strings(news_bytes: bytes):
    links = extract_pdf_links(news_bytes, "https://example.gov/news")
    assert any(link.endswith("remarks-2026-07-01.pdf?v=2") for link in links)


def test_extract_pdf_links_ignores_non_pdf_anchors(news_bytes: bytes):
    links = extract_pdf_links(news_bytes, "https://example.gov/news")
    assert not any(link == "/news/remarks" for link in links)


def test_extract_pdf_links_honours_limit(news_bytes: bytes):
    assert len(extract_pdf_links(news_bytes, "https://example.gov/news", limit=2)) == 2


def test_parse_index_pulls_titles_and_dates(news_bytes: bytes):
    items = parse_index(news_bytes, "https://example.gov/news")
    assert items[0]["title"] == "Quarterly economic outlook"
    assert items[0]["published_at"] == "2026-07-06"
