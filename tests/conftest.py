"""Fixtures. The unit suite must run with no Postgres and no network."""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime
from pathlib import Path

import pytest

from lake.core.models import RunContext
from lake.core.storage import SENTINEL_NAME, Storage


@pytest.fixture
def nas(tmp_path: Path) -> Path:
    """A fake NAS root, complete with the mount sentinel."""
    root = tmp_path / "nas" / "lake"
    root.mkdir(parents=True)
    (root / SENTINEL_NAME).touch()
    return root


@pytest.fixture
def staging(tmp_path: Path) -> Path:
    d = tmp_path / "staging"
    d.mkdir(parents=True)
    return d


@pytest.fixture
def storage(nas: Path, staging: Path) -> Storage:
    # require_sentinel stays on: the mount guard is exactly what we want to test.
    return Storage(nas, staging, require_sentinel=True)


@pytest.fixture
def ctx() -> RunContext:
    return RunContext(
        run_id=uuid.UUID("a1b2c3d4-0000-0000-0000-000000000000"),
        source_id="test_source",
        logical_date=date(2026, 7, 9),
        started_at=datetime(2026, 7, 9, 6, 0, 0, tzinfo=UTC),
    )


class FakeRepo:
    """In-memory stand-in for MetadataRepo. No database, no I/O.

    Records the same call sequence the real repo would see, so a scraper test
    can assert on dedupe decisions without spinning up Postgres.
    """

    def __init__(self) -> None:
        self.runs: dict[uuid.UUID, dict] = {}
        self.files: dict[tuple[str, str], uuid.UUID] = {}  # (source_id, sha256) -> file_id
        self.observations: list[dict] = []
        self.errors: list[dict] = []
        self.succeeded: set[tuple[str, date]] = set()
        self.headers: dict[str, dict[str, str]] = {}

    # -- the MetadataRepo surface that BaseScraper touches --------------------

    def run_succeeded(self, source_id: str, logical_date: date) -> bool:
        return (source_id, logical_date) in self.succeeded

    def start_run(self, ctx: RunContext) -> None:
        self.runs[ctx.run_id] = {"status": "running", "ctx": ctx}

    def finish_run(self, ctx: RunContext, status: str, file_count: int = 0, bytes_written: int = 0):
        self.runs[ctx.run_id] |= {
            "status": status,
            "file_count": file_count,
            "bytes_written": bytes_written,
        }
        if status == "success":
            self.succeeded.add((ctx.source_id, ctx.logical_date))

    def record_error(self, ctx: RunContext, exc: BaseException) -> None:
        self.errors.append({"run_id": ctx.run_id, "error_class": type(exc).__name__, "exc": exc})

    def find_file_by_checksum(self, source_id: str, sha256: str) -> uuid.UUID | None:
        return self.files.get((source_id, sha256))

    def record_file(self, ctx: RunContext, artifact, nas_path: Path) -> uuid.UUID:
        file_id = uuid.uuid4()
        self.files[(ctx.source_id, artifact.sha256)] = file_id
        return file_id

    def record_observation(self, ctx: RunContext, file_id, artifact, was_new: bool) -> None:
        self.observations.append(
            {
                "run_id": ctx.run_id,
                "file_id": file_id,
                "was_new": was_new,
                "sha256": artifact.sha256,
            }
        )

    def last_success_headers(self, source_id: str) -> dict[str, str]:
        return self.headers.get(source_id, {})

    # -- assertion helpers ----------------------------------------------------

    def nth_run(self, index: int) -> dict:
        """Runs in insertion order — dicts preserve it, so tests read chronologically."""
        return list(self.runs.values())[index]

    def last_run(self) -> dict:
        return self.nth_run(-1)


@pytest.fixture
def repo() -> FakeRepo:
    return FakeRepo()
