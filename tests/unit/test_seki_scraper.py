"""The SEKI scraper's fetch(), against a stubbed bi.go.id.

No network. The behaviours under test are the ones that decide whether a month
of Indonesian economic statistics is trustworthy:

  * one flaky table must not discard the other 107,
  * an HTML error page served as `TABEL1_1.xls` must never reach raw/,
  * a site-wide outage must fail the run rather than land a partial month.
"""

from __future__ import annotations

import json
import subprocess
import sys
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest

from lake.core.exceptions import FetchError
from lake.core.models import RunContext
from lake.sources.seki.scraper import SekiScraper

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"

INDEX_URL = "https://www.bi.go.id/id/statistik/ekonomi-keuangan/seki/Default.aspx"
#: The OLE2 compound-file signature every real SEKI .xls starts with.
XLS_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"


@pytest.fixture
def index_html() -> bytes:
    return (FIXTURES / "seki_index.html").read_bytes()


def _ctx() -> RunContext:
    return RunContext(
        run_id=uuid.uuid4(),
        source_id="seki",
        logical_date=date(2026, 7, 1),
        started_at=datetime(2026, 7, 1, tzinfo=UTC),
    )


def _scraper(monkeypatch, handler, **config) -> SekiScraper:
    """A SekiScraper whose httpx.Client is wired to a stub transport."""
    transport = httpx.MockTransport(handler)
    real_client = httpx.Client

    def client(*args, **kwargs):
        kwargs["transport"] = transport
        return real_client(*args, **kwargs)

    monkeypatch.setattr("lake.sources.seki.scraper.httpx.Client", client)

    settings = {
        "index_url": INDEX_URL,
        "retry": {"attempts": 1, "backoff_seconds": 0, "max_backoff_seconds": 0},
        **config,
    }
    return SekiScraper(settings, MagicMock(), MagicMock())


def _handler(index_html: bytes, *, bad: dict[str, httpx.Response] | None = None):
    bad = bad or {}

    def handle(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == INDEX_URL:
            return httpx.Response(200, content=index_html)
        table_id = url.rsplit("/", 1)[-1].removesuffix(".xls")
        if table_id in bad:
            return bad[table_id]
        return httpx.Response(200, content=XLS_MAGIC + b"payload")

    return handle


def test_fetch_lands_the_index_the_catalogue_and_every_table(monkeypatch, index_html):
    scraper = _scraper(monkeypatch, _handler(index_html))
    artifacts = scraper.fetch(_ctx())

    names = [a.filename for a in artifacts]
    assert names[0] == "seki_20260701_index.html"
    assert names[1] == "seki_20260701_catalogue.json"
    assert len(artifacts) == 2 + 5  # index + catalogue + the fixture's five tables
    assert all(n.endswith(".xls") for n in names[2:])
    assert "seki_20260701_TABEL1_1.xls" in names


def test_the_catalogue_carries_titles_and_sections_the_xls_does_not(monkeypatch, index_html):
    """A raw .xls does not know its own name. The index page is the only record
    of what was offered on the day we collected, so it travels with the data."""
    scraper = _scraper(monkeypatch, _handler(index_html))
    catalogue = next(a for a in scraper.fetch(_ctx()) if a.filename.endswith("catalogue.json"))

    entries = json.loads(catalogue.content)
    assert len(entries) == 5
    first = entries[0]
    assert first["table_id"] == "TABEL1_1"
    assert first["number"] == "I.1."
    assert first["section"] == "I. UANG DAN BANK"
    assert first["title"].startswith("Uang Beredar")


def test_one_dead_link_does_not_discard_the_other_tables(monkeypatch, index_html):
    """Bank Indonesia's endpoints are intermittently flaky: a table that answers
    302 with an empty body on one request serves 130 kB of Excel on the next. A
    month of statistics must not be lost to that."""
    empty = httpx.Response(200, content=b"")
    scraper = _scraper(
        monkeypatch, _handler(index_html, bad={"TABEL1_2": empty}), min_success_ratio=0.5
    )

    names = [a.filename for a in scraper.fetch(_ctx())]
    assert "seki_20260701_TABEL1_2.xls" not in names
    assert "seki_20260701_TABEL1_1.xls" in names
    assert len(names) == 2 + 4


def test_an_html_error_page_named_xls_never_reaches_raw(monkeypatch, index_html):
    """The single most common silent failure in scraping. Caught by the bytes,
    not by the extension and not by the Content-Type header."""
    page = httpx.Response(
        200,
        content=b"<!doctype html><html><body>Service unavailable</body></html>",
        headers={"content-type": "application/vnd.ms-excel"},
    )
    scraper = _scraper(
        monkeypatch, _handler(index_html, bad={"TABEL1_3": page}), min_success_ratio=0.5
    )

    names = [a.filename for a in scraper.fetch(_ctx())]
    assert "seki_20260701_TABEL1_3.xls" not in names
    assert len(names) == 2 + 4


def test_a_file_that_is_not_ole2_is_rejected(monkeypatch, index_html):
    """xlsx is a zip; SEKI publishes BIFF. A silently changed format is a change
    we want to hear about, not one to parse six months later."""
    xlsx = httpx.Response(200, content=b"PK\x03\x04not-really-biff")
    scraper = _scraper(
        monkeypatch, _handler(index_html, bad={"TABEL1_4": xlsx}), min_success_ratio=0.5
    )

    names = [a.filename for a in scraper.fetch(_ctx())]
    assert "seki_20260701_TABEL1_4.xls" not in names


def test_a_site_wide_outage_fails_the_run(monkeypatch, index_html):
    """A dead link and an outage look identical one file at a time. The floor is
    what tells them apart, so a partial month never looks like a full one."""
    dead = httpx.Response(500)
    bad = dict.fromkeys(("TABEL1_1", "TABEL1_1_1", "TABEL1_2", "TABEL1_3"), dead)
    scraper = _scraper(monkeypatch, _handler(index_html, bad=bad), min_success_ratio=0.90)

    with pytest.raises(FetchError, match="outage"):
        scraper.fetch(_ctx())


def test_the_floor_is_configurable(monkeypatch, index_html):
    dead = httpx.Response(500)
    bad = dict.fromkeys(("TABEL1_1", "TABEL1_1_1"), dead)
    # 3 of 5 land = 60%, which clears a 50% floor and fails a 90% one
    lenient = _scraper(monkeypatch, _handler(index_html, bad=bad), min_success_ratio=0.5)
    assert len(lenient.fetch(_ctx())) == 2 + 3

    strict = _scraper(monkeypatch, _handler(index_html, bad=bad), min_success_ratio=0.9)
    with pytest.raises(FetchError):
        strict.fetch(_ctx())


def test_an_index_with_no_tables_is_a_failure_not_an_empty_month(monkeypatch):
    """Publishing zero tables is a perfectly successful HTTP 200."""
    scraper = _scraper(monkeypatch, _handler(b"<html><body>maintenance</body></html>"))
    with pytest.raises(FetchError, match="no Excel tables"):
        scraper.fetch(_ctx())


def test_max_tables_limits_the_fan_out(monkeypatch, index_html):
    scraper = _scraper(monkeypatch, _handler(index_html), max_tables=2)
    assert len(scraper.fetch(_ctx())) == 2 + 2


def test_the_scraper_does_not_import_the_parser_dependency():
    """Layering: `lake scrape seki` must run without the transform extra, so
    importing the scraper may never pull in xlrd. Checked in a clean interpreter,
    because this test session has already imported it."""
    probe = (
        "import sys; import lake.sources.seki.scraper; sys.exit(1 if 'xlrd' in sys.modules else 0)"
    )
    assert subprocess.run([sys.executable, "-c", probe], check=False).returncode == 0
