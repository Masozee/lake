"""End-to-end: a real HTTP server, a real filesystem, a fake catalog.

Proves the whole path — fetch, structural gate, checksum, atomic commit, manifest —
without needing Postgres or the network. If this passes, `lake scrape` works.
"""

from __future__ import annotations

import json
import threading
from datetime import date
from functools import partial
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path

import pytest

from lake.core.exceptions import SourceUnchanged, ValidationFailed
from lake.sources.bps_inflation.scraper import BpsInflationScraper
from lake.sources.worldbank_gdp.scraper import WorldBankGDPScraper

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures"


class Handler(SimpleHTTPRequestHandler):
    """Serves fixtures, plus a few deliberately broken endpoints."""

    def log_message(self, *_args):  # silence the test output
        pass

    def do_GET(self):
        if self.path.startswith("/gdp"):
            body = (FIXTURES / "worldbank_gdp_page1.json").read_bytes()
            self._respond(200, body, "application/json", etag='"gdp-v1"')

        elif self.path.startswith("/inflation.xlsx"):
            # Conditional GET: honour If-None-Match like a real server.
            if self.headers.get("If-None-Match") == '"xlsx-v1"':
                self.send_response(304)
                self.send_header("ETag", '"xlsx-v1"')
                self.end_headers()
                return
            # a minimal but genuine xlsx (zip) magic number
            self._respond(
                200, b"PK\x03\x04" + b"\x00" * 128, "application/vnd.ms-excel", etag='"xlsx-v1"'
            )

        elif self.path.startswith("/broken.xlsx"):
            # The classic: HTTP 200 carrying an HTML error page.
            self._respond(200, b"<!DOCTYPE html><html><title>404</title></html>", "text/html")

        else:
            self.send_error(404)

    def _respond(self, code: int, body: bytes, content_type: str, etag: str | None = None):
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        if etag:
            self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(body)


@pytest.fixture(scope="module")
def server():
    httpd = HTTPServer(("127.0.0.1", 0), partial(Handler, directory=str(FIXTURES)))
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{httpd.server_port}"
    httpd.shutdown()


@pytest.mark.integration
def test_api_scraper_lands_bytes_on_the_nas(server, storage, repo, nas):
    config = {
        "base_url": f"{server}/gdp",
        "params": {"format": "json"},
        "timeout_seconds": 10,
        "retry": {"attempts": 2, "backoff_seconds": 0.01, "max_backoff_seconds": 0.05},
    }
    scraper = WorldBankGDPScraper(config, storage, repo)
    ctx = scraper.run(date(2026, 7, 9))

    run_dir = storage.raw_dir(ctx)
    assert storage.is_complete(run_dir)

    written = [p for p in run_dir.glob("worldbank_gdp_*.json") if not p.name.endswith(".meta.json")]
    assert len(written) == 1

    # the bytes on the NAS are byte-identical to what the server sent
    payload = json.loads(written[0].read_bytes())
    assert payload[0]["pages"] == 1
    assert len(payload[1]) == 4

    sidecar = json.loads(written[0].with_name(written[0].name + ".meta.json").read_text())
    assert sidecar["http_status"] == 200
    assert sidecar["etag"] == '"gdp-v1"'
    assert sidecar["sha256"] == repo.observations[0]["sha256"]

    assert repo.last_run()["status"] == "success"


@pytest.mark.integration
def test_rerunning_the_same_date_does_not_refetch(server, storage, repo):
    config = {"base_url": f"{server}/gdp", "params": {}, "timeout_seconds": 10}
    WorldBankGDPScraper(config, storage, repo).run(date(2026, 7, 9))
    WorldBankGDPScraper(config, storage, repo).run(date(2026, 7, 9))

    assert len(repo.runs) == 1, "the second run should have short-circuited on idempotency"


@pytest.mark.integration
def test_conditional_get_yields_skipped_unchanged(server, storage, repo):
    config = {"url": f"{server}/inflation.xlsx", "timeout_seconds": 10}

    ctx1 = BpsInflationScraper(config, storage, repo).run(date(2026, 7, 1))
    assert repo.runs[ctx1.run_id]["status"] == "success"

    # The repo now hands back the ETag, so the second run sends If-None-Match
    # and the server answers 304. Nothing failed; upstream published nothing.
    repo.headers["bps_inflation"] = {"etag": '"xlsx-v1"'}
    ctx2 = BpsInflationScraper(config, storage, repo).run(date(2026, 8, 1))

    assert repo.runs[ctx2.run_id]["status"] == "skipped_unchanged"
    assert repo.errors == []


@pytest.mark.integration
def test_html_error_page_never_reaches_raw(server, storage, repo, nas):
    """A 200 that is really a 404 page. The gate catches it; raw/ stays clean."""
    config = {"url": f"{server}/broken.xlsx", "timeout_seconds": 10}
    scraper = BpsInflationScraper(config, storage, repo)

    with pytest.raises(ValidationFailed, match="HTML page"):
        scraper.run(date(2026, 7, 1))

    assert not list((nas / "raw").rglob("*.xlsx"))
    assert len(list((nas / "quarantine").rglob("_FAILURE_*.json"))) == 1
    assert repo.last_run()["status"] == "failed"


@pytest.mark.integration
def test_unreachable_host_fails_the_run_and_quarantines(storage, repo, nas):
    config = {
        "url": "http://127.0.0.1:1/nothing.xlsx",
        "timeout_seconds": 1,
        "retry": {"attempts": 2, "backoff_seconds": 0.01, "max_backoff_seconds": 0.02},
    }
    scraper = BpsInflationScraper(config, storage, repo)

    with pytest.raises(Exception):  # noqa: B017 — httpx.ConnectError, exact type is not the point
        scraper.run(date(2026, 7, 1))

    assert repo.last_run()["status"] == "failed"
    assert repo.errors[0]["error_class"] in {"ConnectError", "ConnectionRefusedError"}


@pytest.mark.integration
def test_source_unchanged_is_not_an_error_type(storage, repo):
    """SourceUnchanged deliberately does not inherit from a transient error."""
    assert not SourceUnchanged("304").transient
