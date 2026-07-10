"""The scrape lifecycle. Every source inherits this and implements only fetch().

Layering is strict, and the payoff is testability:

    config.yaml   what to fetch. no logic.
    scraper.py    bytes in from the network. no parsing, no disk.
    storage.py    bytes to the NAS, atomically. shared, not per-source.
    parser.py     bytes -> list[dict]. pure function: no network, no disk.
    schema.py     dict -> validated record, or rejected.
    transform.py  validated records -> parquet.

Because parser.py is pure, you test it against captured fixtures with no network.
Because storage.py is shared, the atomicity bug gets fixed exactly once.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import structlog

from lake.core.exceptions import SourceUnchanged
from lake.core.logging import get_logger
from lake.core.models import Artifact, RunContext
from lake.core.storage import Storage
from lake.core.validate import check_all
from lake.metadata.repo import MetadataRepo

log = get_logger(__name__)


class BaseScraper(ABC):
    #: matches the `source_id` in configs/sources.yaml
    source_id: str = ""
    #: daily | weekly | monthly | yearly
    schedule: str = "daily"

    def __init__(self, config: dict[str, Any], storage: Storage, meta: MetadataRepo):
        if not self.source_id:
            raise ValueError(f"{type(self).__name__} must set source_id")
        self.config = config
        self.storage = storage
        self.meta = meta

    # -- to implement ---------------------------------------------------------

    @abstractmethod
    def fetch(self, ctx: RunContext) -> list[Artifact]:
        """Pull bytes from upstream. Raise on failure — the caller records it.

        Raise SourceUnchanged to mark the run 'skipped_unchanged' (e.g. HTTP 304).
        In-run retries belong here, via lake.core.retry.retrying().
        """

    def prior_conditional_headers(self) -> dict[str, str]:
        """ETag / If-Modified-Since from the last success. Saves bandwidth and
        turns 'source published nothing' into an explicit 304."""
        prev = self.meta.last_success_headers(self.source_id)
        headers: dict[str, str] = {}
        if prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]
        return headers

    @property
    def user_agent(self) -> str:
        return self.config.get("user_agent", "lake/1.0")

    @property
    def timeout(self) -> float:
        return float(self.config.get("timeout_seconds", 60))

    # -- lifecycle ------------------------------------------------------------

    def run(
        self,
        logical_date: date | None = None,
        *,
        force: bool = False,
        attempt: int = 1,
        trigger: str = "manual",
    ) -> RunContext:
        logical_date = logical_date or datetime.now(UTC).date()
        ctx = RunContext.new(self.source_id, logical_date, attempt=attempt, trigger=trigger)

        structlog.contextvars.bind_contextvars(
            run_id=str(ctx.run_id),
            source_id=ctx.source_id,
            logical_date=logical_date.isoformat(),
            attempt=attempt,
        )
        try:
            return self._run_inner(ctx, force=force)
        finally:
            structlog.contextvars.clear_contextvars()

    def _run_inner(self, ctx: RunContext, *, force: bool) -> RunContext:
        # Dedupe layer 1: idempotency on (source_id, logical_date).
        # A DB partial unique index enforces this even under a race.
        if not force and self.meta.run_succeeded(ctx.source_id, ctx.logical_date):
            log.info("run.skipped_already_succeeded")
            return ctx

        self.meta.start_run(ctx)
        committed: list[Path] = []
        bytes_written = 0

        try:
            artifacts = self.fetch(ctx)

            # Structural gate, before a single byte reaches raw/. Catches the
            # classic 404-HTML-page-named-report.xlsx. Sources cannot opt out.
            check_all(artifacts)

            for art in artifacts:
                # Dedupe layer 2: identical bytes we already hold. We still record
                # the *observation*, which is how we tell "source published nothing"
                # apart from "our scraper never ran".
                existing = self.meta.find_file_by_checksum(ctx.source_id, art.sha256)
                if existing and not force:
                    self.meta.record_observation(ctx, existing, art, was_new=False)
                    log.info(
                        "file.skipped_duplicate",
                        filename=art.filename,
                        sha256=art.sha256[:12],
                    )
                    continue

                size = art.size_bytes  # read before commit; StreamedFile source may move
                rel_path = self.storage.commit(ctx, art)
                file_id = self.meta.record_file(ctx, art, rel_path)
                self.meta.record_observation(ctx, file_id, art, was_new=True)
                committed.append(rel_path)
                bytes_written += size

            self.storage.write_manifest(ctx, committed, status="complete")
            self.storage.cleanup_staging(ctx)

            self.meta.finish_run(
                ctx, status="success", file_count=len(committed), bytes_written=bytes_written
            )
            log.info("run.success", files_written=len(committed), bytes_written=bytes_written)

        except SourceUnchanged:
            # Dedupe layer 3: conditional GET said 304. Not a failure.
            self.storage.cleanup_staging(ctx)
            self.meta.finish_run(ctx, status="skipped_unchanged")
            log.info("run.skipped_unchanged")

        except BaseException as exc:
            self.storage.quarantine(ctx, exc)
            self.meta.record_error(ctx, exc)
            self.meta.finish_run(ctx, status="failed")
            log.exception("run.failed", error_class=type(exc).__name__)
            raise

        return ctx
