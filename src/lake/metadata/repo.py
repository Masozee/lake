"""The only module that talks to Postgres.

Each method opens its own short transaction and commits. A scraper that dies
mid-fetch leaves a durable `running` row and its `run_errors`, which is exactly
what you want when you come back to debug it.
"""

from __future__ import annotations

import os
import socket
import subprocess
import traceback
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import httpx
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from lake.core.exceptions import LakeError
from lake.core.models import Artifact, RunContext
from lake.metadata.models import (
    Dataset,
    File,
    FileObservation,
    Run,
    RunError,
    RunStatus,
    Source,
    Validation,
)
from lake.metadata.session import session_scope


def _git_sha() -> str | None:
    """Which code produced this data. Worth the 5ms."""
    if sha := os.environ.get("LAKE_GIT_SHA"):
        return sha[:40]
    try:
        out = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            timeout=2,
            cwd=Path(__file__).resolve().parents[3],
        )
        return out.stdout.strip()[:40] or None
    except (OSError, subprocess.SubprocessError):
        return None


def _http_status_of(exc: BaseException) -> int | None:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code
    return getattr(exc, "http_status", None)


class MetadataRepo:
    # -- sources --------------------------------------------------------------

    def upsert_source(self, cfg: dict[str, Any]) -> None:
        """Sync one entry of configs/sources.yaml into the catalog."""
        retention = cfg.get("retention", {}) or {}
        values = {
            "source_id": cfg["source_id"],
            "display_name": cfg.get("display_name", cfg["source_id"]),
            "kind": cfg.get("kind", "api"),
            "schedule": cfg.get("schedule", "daily"),
            "enabled": cfg.get("enabled", True),
            "base_url": cfg.get("base_url") or cfg.get("url") or cfg.get("index_url"),
            "owner": cfg.get("owner"),
            "freshness_sla_hours": cfg.get("freshness_sla_hours"),
            "retention_raw_days": retention.get("raw_days"),
            "config": cfg,
        }
        with session_scope() as s:
            stmt = pg_insert(Source).values(**values)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Source.source_id],
                    set_={k: v for k, v in values.items() if k != "source_id"},
                )
            )

    def list_sources(self, schedule: str | None = None, enabled_only: bool = True) -> list[Source]:
        with session_scope() as s:
            q = select(Source)
            if schedule:
                q = q.where(Source.schedule == schedule)
            if enabled_only:
                q = q.where(Source.enabled.is_(True))
            return list(s.scalars(q.order_by(Source.source_id)))

    # -- runs -----------------------------------------------------------------

    def run_succeeded(self, source_id: str, logical_date: date) -> bool:
        """Dedupe layer 1. Backed by the partial unique index, not just this check."""
        with session_scope() as s:
            return (
                s.scalar(
                    select(Run.run_id).where(
                        Run.source_id == source_id,
                        Run.logical_date == logical_date,
                        Run.status == RunStatus.success,
                    )
                )
                is not None
            )

    def next_attempt(self, source_id: str, logical_date: date) -> int:
        with session_scope() as s:
            highest = s.scalar(
                select(Run.attempt)
                .where(Run.source_id == source_id, Run.logical_date == logical_date)
                .order_by(Run.attempt.desc())
                .limit(1)
            )
            return (highest or 0) + 1

    def start_run(self, ctx: RunContext) -> None:
        with session_scope() as s:
            s.add(
                Run(
                    run_id=ctx.run_id,
                    source_id=ctx.source_id,
                    logical_date=ctx.logical_date,
                    attempt=ctx.attempt,
                    status=RunStatus.running,
                    trigger=ctx.trigger,
                    started_at=ctx.started_at,
                    host=socket.gethostname(),
                    git_sha=_git_sha(),
                )
            )

    def finish_run(
        self,
        ctx: RunContext,
        status: str,
        file_count: int = 0,
        bytes_written: int = 0,
    ) -> None:
        finished = datetime.now(UTC)
        duration_ms = int((finished - ctx.started_at).total_seconds() * 1000)
        with session_scope() as s:
            s.execute(
                update(Run)
                .where(Run.run_id == ctx.run_id)
                .values(
                    status=RunStatus(status),
                    finished_at=finished,
                    duration_ms=duration_ms,
                    file_count=file_count,
                    bytes_written=bytes_written,
                )
            )

    def record_error(self, ctx: RunContext, exc: BaseException) -> None:
        with session_scope() as s:
            s.add(
                RunError(
                    run_id=ctx.run_id,
                    error_class=type(exc).__name__,
                    error_message=str(exc)[:4000],
                    http_status=_http_status_of(exc),
                    url=getattr(exc, "url", None),
                    traceback="".join(
                        traceback.format_exception(type(exc), exc, exc.__traceback__)
                    )[:20000],
                    is_transient=bool(getattr(exc, "transient", False))
                    if isinstance(exc, LakeError)
                    else isinstance(exc, httpx.TransportError),
                )
            )

    # -- retry ----------------------------------------------------------------

    def failed_runs_to_retry(
        self,
        max_attempts: int = 3,
        older_than_minutes: int = 15,
        stale_after_hours: int = 6,
    ) -> list[dict]:
        """Cross-run retry candidates: failed, under the attempt cap, not superseded.

        Excludes any (source, logical_date) that has since succeeded — a manual
        re-run must not be clobbered by the retry timer. Only the latest *failed*
        attempt of each (source, logical_date) is eligible, so attempts advance
        1→2→3 rather than fanning out.

        Two subtleties, both learned the hard way:

        * `m.status = 'failed'` inside the max(attempt) subquery. Without it, a
          run killed mid-flight (status still 'running', finished_at NULL) becomes
          the max attempt, matches no branch of the outer filter, and silently
          blocks that logical_date from ever being retried again.

        * The 'running' exclusion is time-bounded. An in-flight run must block a
          retry (never run the same scrape twice at once), but a run that has been
          'running' for six hours is a corpse, not a worker — otherwise a single
          `kill -9` freezes that logical_date forever.
        """
        sql = text(
            """
            SELECT r.source_id, r.logical_date, r.attempt
            FROM runs r
            WHERE r.status = 'failed'
              AND r.attempt < :max_attempts
              AND r.finished_at < now() - make_interval(mins => :older_than)
              AND NOT EXISTS (
                    SELECT 1 FROM runs done
                    WHERE done.source_id = r.source_id
                      AND done.logical_date = r.logical_date
                      AND done.status IN ('success', 'skipped_unchanged'))
              AND NOT EXISTS (
                    SELECT 1 FROM runs live
                    WHERE live.source_id = r.source_id
                      AND live.logical_date = r.logical_date
                      AND live.status = 'running'
                      AND live.started_at > now() - make_interval(hours => :stale_after_hours))
              AND r.attempt = (
                    SELECT max(m.attempt) FROM runs m
                    WHERE m.source_id = r.source_id
                      AND m.logical_date = r.logical_date
                      AND m.status = 'failed')
            ORDER BY r.finished_at
            """
        )
        with session_scope() as s:
            rows = s.execute(
                sql,
                {
                    "max_attempts": max_attempts,
                    "older_than": older_than_minutes,
                    "stale_after_hours": stale_after_hours,
                },
            ).mappings()
            return [dict(r) for r in rows]

    # -- files ----------------------------------------------------------------

    def find_file_by_checksum(self, source_id: str, sha256: str) -> uuid.UUID | None:
        """Dedupe layer 2. Identical bytes we already hold."""
        with session_scope() as s:
            return s.scalar(
                select(File.file_id).where(
                    File.source_id == source_id,
                    File.sha256 == sha256,
                    File.deleted_at.is_(None),
                )
            )

    def record_file(self, ctx: RunContext, artifact: Artifact, nas_path: Path) -> uuid.UUID:
        file_id = uuid.uuid4()
        with session_scope() as s:
            stmt = (
                pg_insert(File)
                .values(
                    file_id=file_id,
                    source_id=ctx.source_id,
                    sha256=artifact.sha256,
                    size_bytes=artifact.size_bytes,
                    content_type=artifact.content_type,
                    extension=Path(artifact.filename).suffix.lstrip("."),
                    nas_path=str(nas_path),
                    layer="raw",
                )
                .on_conflict_do_nothing(constraint="files_source_sha")
                .returning(File.file_id)
            )
            returned = s.scalar(stmt)
            if returned:
                return returned
            # Lost a race with a concurrent run; adopt the existing row.
            existing = s.scalar(
                select(File.file_id).where(
                    File.source_id == ctx.source_id, File.sha256 == artifact.sha256
                )
            )
            assert existing is not None
            return existing

    def record_observation(
        self, ctx: RunContext, file_id: uuid.UUID, artifact: Artifact, was_new: bool
    ) -> None:
        with session_scope() as s:
            s.execute(
                pg_insert(FileObservation)
                .values(
                    run_id=ctx.run_id,
                    file_id=file_id,
                    was_new=was_new,
                    url=artifact.url,
                    http_status=artifact.http_status,
                    etag=artifact.etag,
                    last_modified=artifact.last_modified,
                )
                .on_conflict_do_nothing(constraint="file_obs_run_file")
            )

    def last_success_headers(self, source_id: str) -> dict[str, str]:
        """ETag / Last-Modified from the newest successful observation.

        Dedupe layer 3: feed these back as conditional-GET headers.
        """
        sql = text(
            """
            SELECT o.etag, o.last_modified
            FROM file_observations o
            JOIN runs r ON r.run_id = o.run_id
            WHERE r.source_id = :sid AND r.status = 'success'
            ORDER BY o.observed_at DESC
            LIMIT 1
            """
        )
        with session_scope() as s:
            row = s.execute(sql, {"sid": source_id}).mappings().first()
            if not row:
                return {}
            return {k: v for k, v in dict(row).items() if v}

    def files_for_retention(self, source_id: str, older_than_days: int, layer: str = "raw"):
        sql = text(
            """
            SELECT file_id, nas_path, size_bytes
            FROM files
            WHERE source_id = :sid AND layer = :layer AND deleted_at IS NULL
              AND first_seen_at < now() - make_interval(days => :days)
            ORDER BY first_seen_at
            """
        )
        with session_scope() as s:
            return list(
                s.execute(
                    sql, {"sid": source_id, "layer": layer, "days": older_than_days}
                ).mappings()
            )

    def mark_deleted(self, file_ids: list[uuid.UUID]) -> None:
        if not file_ids:
            return
        with session_scope() as s:
            s.execute(
                update(File).where(File.file_id.in_(file_ids)).values(deleted_at=datetime.now(UTC))
            )

    def mark_archived(self, file_ids: list[uuid.UUID]) -> None:
        if not file_ids:
            return
        with session_scope() as s:
            s.execute(
                update(File)
                .where(File.file_id.in_(file_ids))
                .values(archived_at=datetime.now(UTC), layer="archive")
            )

    # -- validations & datasets ----------------------------------------------

    def record_validation(
        self,
        run_id: uuid.UUID | None,
        file_id: uuid.UUID | None,
        check_name: str,
        passed: bool,
        rows_total: int | None = None,
        rows_rejected: int | None = None,
        detail: dict | None = None,
    ) -> None:
        with session_scope() as s:
            s.add(
                Validation(
                    run_id=run_id,
                    file_id=file_id,
                    check_name=check_name,
                    passed=passed,
                    rows_total=rows_total,
                    rows_rejected=rows_rejected,
                    detail=detail,
                )
            )

    def dataset_row_history(self, dataset_id: str, limit: int = 12) -> list[int]:
        """Row counts of recent successful builds, newest last.

        Feeds the 3-sigma row-count gate. Fewer than four points and the gate only
        asserts non-empty — a standard deviation from three samples is worse than
        no interval at all.
        """
        sql = text(
            """
            SELECT rows_total
            FROM validations
            WHERE check_name = 'row_count_sane' AND passed AND rows_total IS NOT NULL
              AND detail->>'dataset_id' = :dataset_id
            ORDER BY checked_at DESC
            LIMIT :limit
            """
        )
        with session_scope() as s:
            rows = [r[0] for r in s.execute(sql, {"dataset_id": dataset_id, "limit": limit})]
        return list(reversed(rows))

    def record_dataset(
        self,
        dataset_id: str,
        source_id: str,
        nas_path: str,
        row_count: int,
        partition_keys: list[str] | None = None,
        built_from_run: uuid.UUID | None = None,
    ) -> None:
        values = {
            "dataset_id": dataset_id,
            "source_id": source_id,
            "nas_path": nas_path,
            "format": "parquet",
            "partition_keys": partition_keys,
            "row_count": row_count,
            "built_from_run": built_from_run,
            "built_at": datetime.now(UTC),
        }
        with session_scope() as s:
            stmt = pg_insert(Dataset).values(**values)
            s.execute(
                stmt.on_conflict_do_update(
                    index_elements=[Dataset.dataset_id],
                    set_={k: v for k, v in values.items() if k != "dataset_id"},
                )
            )

    # -- health ---------------------------------------------------------------

    def freshness(self) -> list[dict]:
        with session_scope() as s:
            return [dict(r) for r in s.execute(text("SELECT * FROM v_freshness")).mappings()]

    def stale_sources(self) -> list[dict]:
        """The alert that catches a scraper which silently stopped being scheduled.

        OnFailure= structurally cannot see this: nothing failed, nothing ran.
        """
        # NULLS FIRST: a source that has never once succeeded is the most urgent
        # thing on the list, and its hours_since_success is NULL, not infinity.
        sql = text(
            """
            SELECT * FROM v_freshness
            WHERE is_stale
            ORDER BY hours_since_success DESC NULLS FIRST
            """
        )
        with session_scope() as s:
            return [dict(r) for r in s.execute(sql).mappings()]

    def recent_runs(self, source_id: str | None = None, limit: int = 20) -> list[dict]:
        sql = """
            SELECT source_id, logical_date, status, attempt, trigger,
                   duration_ms, file_count, bytes_written, started_at
            FROM runs
            {where}
            ORDER BY started_at DESC
            LIMIT :limit
        """.format(where="WHERE source_id = :sid" if source_id else "")
        params: dict[str, Any] = {"limit": limit}
        if source_id:
            params["sid"] = source_id
        with session_scope() as s:
            return [dict(r) for r in s.execute(text(sql), params).mappings()]
