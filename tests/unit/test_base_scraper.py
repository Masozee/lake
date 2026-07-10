"""The scrape lifecycle: idempotency, checksum dedupe, 304 handling, quarantine."""

from __future__ import annotations

import json
from datetime import date

import pytest

from lake.core.base_scraper import BaseScraper
from lake.core.exceptions import SourceUnchanged, ValidationFailed
from lake.core.models import Artifact, FetchedFile, RunContext


class StubScraper(BaseScraper):
    source_id = "test_source"
    schedule = "daily"

    def __init__(self, config, storage, meta, artifacts=None, raises=None):
        super().__init__(config, storage, meta)
        self._artifacts = artifacts if artifacts is not None else [_json_file()]
        self._raises = raises
        self.fetch_calls = 0

    def fetch(self, ctx: RunContext) -> list[Artifact]:
        self.fetch_calls += 1
        if self._raises:
            raise self._raises
        return self._artifacts


def _json_file(content: bytes = b'{"rows": [1, 2, 3]}', name="test_source_20260709_000.json"):
    return FetchedFile(filename=name, content=content, url="https://example.org/x", http_status=200)


@pytest.fixture
def scraper(storage, repo):
    return StubScraper({}, storage, repo)


def test_successful_run_commits_and_records(scraper, repo, nas):
    ctx = scraper.run(date(2026, 7, 9))

    assert repo.runs[ctx.run_id]["status"] == "success"
    assert repo.runs[ctx.run_id]["file_count"] == 1
    assert repo.runs[ctx.run_id]["bytes_written"] == len(b'{"rows": [1, 2, 3]}')
    assert repo.observations[0]["was_new"] is True


def test_second_run_of_same_date_is_skipped(scraper, repo):
    """Dedupe layer 1: idempotency on (source_id, logical_date)."""
    scraper.run(date(2026, 7, 9))
    assert scraper.fetch_calls == 1

    scraper.run(date(2026, 7, 9))
    assert scraper.fetch_calls == 1, "fetch() ran again for an already-successful date"


def test_force_overrides_idempotency(scraper, repo):
    scraper.run(date(2026, 7, 9))
    scraper.run(date(2026, 7, 9), force=True)
    assert scraper.fetch_calls == 2


def test_identical_bytes_are_not_rewritten(storage, repo):
    """Dedupe layer 2: same checksum, different logical_date. Skip the write,
    but still record the observation — that is how we tell 'source went quiet'
    from 'our scraper broke'."""
    s1 = StubScraper({}, storage, repo)
    s1.run(date(2026, 7, 9))

    s2 = StubScraper({}, storage, repo)  # same bytes, next day
    s2.run(date(2026, 7, 10))

    assert [o["was_new"] for o in repo.observations] == [True, False]
    assert repo.nth_run(0)["file_count"] == 1
    assert repo.nth_run(1)["file_count"] == 0  # the duplicate was never rewritten


def test_run_that_writes_nothing_still_gets_a_manifest(storage, repo, nas):
    """`file_count: 0` is the durable record that we checked and found nothing new.

    Without it the second run's directory looks like a crash to every downstream
    reader, and 'source published nothing' becomes indistinguishable from 'the
    scraper died mid-write'.
    """
    StubScraper({}, storage, repo).run(date(2026, 7, 9))
    s2 = StubScraper({}, storage, repo)
    ctx = s2.run(date(2026, 7, 10))

    run_dir = storage.raw_dir(ctx)
    assert storage.is_complete(run_dir)
    assert json.loads((run_dir / "_MANIFEST.json").read_text())["file_count"] == 0
    assert list(run_dir.iterdir()) == [run_dir / "_MANIFEST.json"]


def test_source_unchanged_is_a_skip_not_a_failure(storage, repo):
    """Dedupe layer 3: HTTP 304. Nothing failed; upstream published nothing."""
    scraper = StubScraper({}, storage, repo, raises=SourceUnchanged("304"))
    ctx = scraper.run(date(2026, 7, 9))

    assert repo.runs[ctx.run_id]["status"] == "skipped_unchanged"
    assert repo.errors == []


def test_failure_quarantines_and_records_error(storage, repo, nas):
    scraper = StubScraper({}, storage, repo, raises=RuntimeError("upstream exploded"))

    with pytest.raises(RuntimeError, match="upstream exploded"):
        scraper.run(date(2026, 7, 9))

    assert repo.last_run()["status"] == "failed"
    assert repo.errors[0]["error_class"] == "RuntimeError"

    failures = list((nas / "quarantine").rglob("_FAILURE_*.json"))
    assert len(failures) == 1


def test_html_error_page_named_json_is_rejected(storage, repo, nas):
    """The single most common silent failure: a 200 that is really an error page."""
    html = b"<!DOCTYPE html><html><head><title>404 Not Found</title></head></html>"
    scraper = StubScraper({}, storage, repo, artifacts=[_json_file(content=html)])

    with pytest.raises(ValidationFailed, match="HTML page"):
        scraper.run(date(2026, 7, 9))

    assert not list((nas / "raw").rglob("*.json")), "an HTML error page reached raw/"


def test_empty_result_is_rejected(storage, repo):
    """Publishing zero artifacts is a perfectly successful HTTP 200."""
    scraper = StubScraper({}, storage, repo, artifacts=[])

    with pytest.raises(ValidationFailed, match="zero artifacts"):
        scraper.run(date(2026, 7, 9))


def test_conditional_headers_come_from_last_success(storage, repo):
    repo.headers["test_source"] = {
        "etag": '"abc123"',
        "last_modified": "Wed, 08 Jul 2026 23:00:00 GMT",
    }
    scraper = StubScraper({}, storage, repo)

    headers = scraper.prior_conditional_headers()
    assert headers["If-None-Match"] == '"abc123"'
    assert headers["If-Modified-Since"] == "Wed, 08 Jul 2026 23:00:00 GMT"
