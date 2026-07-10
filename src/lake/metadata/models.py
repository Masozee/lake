"""SQLAlchemy ORM — the metadata catalog.

Design notes worth remembering at 3am:

* `runs` has a partial unique index on (source_id, logical_date) WHERE
  status='success'. That is the idempotency guard: the database, not the
  application, guarantees one successful run per logical date.

* `files` is keyed by content: (source_id, sha256) is unique. `file_observations`
  is the many-to-many between runs and files, carrying `was_new`. That column is
  the whole reason this table exists — it separates "the source stopped
  publishing" from "our scraper broke". Different alerts, different fixes.
  Most catalogs conflate the two and you can never tell which happened.

* `runs.git_sha` lets you check out the exact code that produced a file and
  replay it against the exact bytes in `files.nas_path`.
"""

from __future__ import annotations

import enum
import uuid
from datetime import date, datetime

from sqlalchemy import (
    ARRAY,
    BigInteger,
    Boolean,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    SmallInteger,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.dialects.postgresql import UUID as PGUUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class RunStatus(str, enum.Enum):
    pending = "pending"
    running = "running"
    success = "success"
    failed = "failed"
    skipped_unchanged = "skipped_unchanged"
    partial = "partial"


class ScheduleKind(str, enum.Enum):
    daily = "daily"
    weekly = "weekly"
    monthly = "monthly"
    yearly = "yearly"
    adhoc = "adhoc"


_run_status = Enum(RunStatus, name="run_status", values_callable=lambda e: [m.value for m in e])
_schedule_kind = Enum(
    ScheduleKind, name="schedule_kind", values_callable=lambda e: [m.value for m in e]
)


class Source(Base):
    """Registry of every source. Mirrors configs/sources.yaml, synced by `lake sync-sources`."""

    __tablename__ = "sources"

    source_id: Mapped[str] = mapped_column(Text, primary_key=True)
    display_name: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # api | html | file
    schedule: Mapped[ScheduleKind] = mapped_column(_schedule_kind, nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    base_url: Mapped[str | None] = mapped_column(Text)
    owner: Mapped[str | None] = mapped_column(Text)
    #: alert when now() - last_success exceeds this. Catches the silent stop.
    freshness_sla_hours: Mapped[int | None] = mapped_column(Integer)
    retention_raw_days: Mapped[int | None] = mapped_column(Integer)
    config: Mapped[dict] = mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )

    runs: Mapped[list[Run]] = relationship(back_populates="source", cascade="all, delete-orphan")


class Run(Base):
    """One scrape attempt. (source_id, logical_date, attempt) is unique."""

    __tablename__ = "runs"

    run_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.source_id", ondelete="CASCADE"), nullable=False
    )
    #: the date the data is ABOUT, not when we fetched it. Backfills stay correct.
    logical_date: Mapped[date] = mapped_column(Date, nullable=False)
    attempt: Mapped[int] = mapped_column(SmallInteger, nullable=False, default=1)
    status: Mapped[RunStatus] = mapped_column(
        _run_status, nullable=False, default=RunStatus.pending
    )
    trigger: Mapped[str] = mapped_column(Text, nullable=False, default="schedule")
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    file_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    bytes_written: Mapped[int] = mapped_column(BigInteger, nullable=False, default=0)
    host: Mapped[str | None] = mapped_column(Text)
    #: which commit produced this data. Reproduce a bad parse exactly.
    git_sha: Mapped[str | None] = mapped_column(String(40))

    source: Mapped[Source] = relationship(back_populates="runs")
    errors: Mapped[list[RunError]] = relationship(
        back_populates="run", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("source_id", "logical_date", "attempt", name="runs_source_date_attempt"),
        # THE idempotency guard: at most one success per (source, logical_date).
        Index(
            "runs_one_success",
            "source_id",
            "logical_date",
            unique=True,
            postgresql_where=text("status = 'success'"),
        ),
        Index("runs_recent", "source_id", text("started_at DESC")),
        Index(
            "runs_failed",
            "status",
            text("started_at DESC"),
            postgresql_where=text("status = 'failed'"),
        ),
    )


class RunError(Base):
    """A run may hit several errors. Keep them all; the first is rarely the cause."""

    __tablename__ = "run_errors"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    error_class: Mapped[str] = mapped_column(Text, nullable=False)
    error_message: Mapped[str] = mapped_column(Text, nullable=False)
    http_status: Mapped[int | None] = mapped_column(Integer)
    url: Mapped[str | None] = mapped_column(Text)
    traceback: Mapped[str | None] = mapped_column(Text)
    is_transient: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    run: Mapped[Run] = relationship(back_populates="errors")

    __table_args__ = (Index("run_errors_class", "error_class", text("occurred_at DESC")),)


class File(Base):
    """A physical artifact on the NAS. Identity is its sha256."""

    __tablename__ = "files"

    file_id: Mapped[uuid.UUID] = mapped_column(PGUUID(as_uuid=True), primary_key=True)
    source_id: Mapped[str] = mapped_column(
        ForeignKey("sources.source_id", ondelete="CASCADE"), nullable=False
    )
    sha256: Mapped[str] = mapped_column(String(64), nullable=False)
    size_bytes: Mapped[int] = mapped_column(BigInteger, nullable=False)
    content_type: Mapped[str | None] = mapped_column(Text)
    extension: Mapped[str | None] = mapped_column(Text)
    #: relative to LAKE_NAS_ROOT, so the lake can be remounted anywhere
    nas_path: Mapped[str] = mapped_column(Text, nullable=False, unique=True)
    layer: Mapped[str] = mapped_column(Text, nullable=False, default="raw")
    first_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    archived_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    #: soft delete. Retention unlinks bytes but keeps the record of what existed.
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint("source_id", "sha256", name="files_source_sha"),
        Index("files_sha", "sha256"),
        Index("files_live", "source_id", "layer", postgresql_where=text("deleted_at IS NULL")),
    )


class FileObservation(Base):
    """Run X saw file Y. Many runs may observe the same unchanged file.

    `was_new=False` for 30 days means the SOURCE went quiet.
    A `failed` run means WE went broken. Never confuse the two.
    """

    __tablename__ = "file_observations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("runs.run_id", ondelete="CASCADE"), nullable=False
    )
    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.file_id", ondelete="CASCADE"), nullable=False
    )
    was_new: Mapped[bool] = mapped_column(Boolean, nullable=False)
    url: Mapped[str | None] = mapped_column(Text)
    http_status: Mapped[int | None] = mapped_column(Integer)
    etag: Mapped[str | None] = mapped_column(Text)
    last_modified: Mapped[str | None] = mapped_column(Text)
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    __table_args__ = (UniqueConstraint("run_id", "file_id", name="file_obs_run_file"),)


class Validation(Base):
    """Structural, schema, and statistical check results. Trend rows_rejected."""

    __tablename__ = "validations"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    run_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.run_id", ondelete="CASCADE"))
    file_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("files.file_id", ondelete="CASCADE")
    )
    check_name: Mapped[str] = mapped_column(Text, nullable=False)
    passed: Mapped[bool] = mapped_column(Boolean, nullable=False)
    rows_total: Mapped[int | None] = mapped_column(BigInteger)
    rows_rejected: Mapped[int | None] = mapped_column(BigInteger)
    detail: Mapped[dict | None] = mapped_column(JSONB)
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Dataset(Base):
    """A processed Parquet output, with lineage back to the run that built it."""

    __tablename__ = "datasets"

    dataset_id: Mapped[str] = mapped_column(Text, primary_key=True)
    source_id: Mapped[str | None] = mapped_column(ForeignKey("sources.source_id"))
    nas_path: Mapped[str] = mapped_column(Text, nullable=False)
    format: Mapped[str] = mapped_column(Text, nullable=False, default="parquet")
    partition_keys: Mapped[list[str] | None] = mapped_column(ARRAY(Text))
    row_count: Mapped[int | None] = mapped_column(BigInteger)
    built_from_run: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("runs.run_id"))
    built_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
