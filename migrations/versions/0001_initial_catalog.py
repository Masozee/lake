"""initial metadata catalog

Revision ID: 0001
Revises:
Create Date: 2026-07-09

Everything the lake knows about itself: sources, runs, errors, files, the
run<->file observation table, validations, datasets, and the v_freshness view
that drives alerting.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


# The alert that catches a scraper which silently stopped being scheduled.
# OnFailure= cannot see this: nothing failed, because nothing ran.
V_FRESHNESS = """
CREATE OR REPLACE VIEW v_freshness AS
SELECT
    s.source_id,
    s.display_name,
    s.schedule,
    s.enabled,
    s.freshness_sla_hours,
    r.last_success_at,
    r.last_status,
    EXTRACT(EPOCH FROM (now() - r.last_success_at)) / 3600.0 AS hours_since_success,
    CASE
        WHEN s.freshness_sla_hours IS NULL THEN FALSE
        WHEN r.last_success_at IS NULL THEN TRUE
        ELSE EXTRACT(EPOCH FROM (now() - r.last_success_at)) / 3600.0
             > s.freshness_sla_hours
    END AS is_stale
FROM sources s
LEFT JOIN LATERAL (
    SELECT
        max(finished_at) FILTER (WHERE status = 'success')  AS last_success_at,
        (array_agg(status ORDER BY started_at DESC))[1]     AS last_status
    FROM runs
    WHERE source_id = s.source_id
) r ON TRUE
WHERE s.enabled;
"""


def upgrade() -> None:
    # create_type=False is load-bearing. Without it SQLAlchemy emits a second
    # CREATE TYPE inside the first CREATE TABLE that references the enum, and
    # Postgres raises DuplicateObject. `checkfirst` only guards the explicit
    # .create() call below, not the implicit one attached to the table DDL.
    run_status = postgresql.ENUM(
        "pending",
        "running",
        "success",
        "failed",
        "skipped_unchanged",
        "partial",
        name="run_status",
        create_type=False,
    )
    schedule_kind = postgresql.ENUM(
        "daily", "weekly", "monthly", "yearly", "adhoc", name="schedule_kind", create_type=False
    )
    run_status.create(op.get_bind(), checkfirst=True)
    schedule_kind.create(op.get_bind(), checkfirst=True)

    op.create_table(
        "sources",
        sa.Column("source_id", sa.Text(), primary_key=True),
        sa.Column("display_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("schedule", schedule_kind, nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("base_url", sa.Text()),
        sa.Column("owner", sa.Text()),
        sa.Column("freshness_sla_hours", sa.Integer()),
        sa.Column("retention_raw_days", sa.Integer()),
        sa.Column(
            "config", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "runs",
        sa.Column("run_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            sa.Text(),
            sa.ForeignKey("sources.source_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("logical_date", sa.Date(), nullable=False),
        sa.Column("attempt", sa.SmallInteger(), nullable=False, server_default="1"),
        sa.Column("status", run_status, nullable=False, server_default="pending"),
        sa.Column("trigger", sa.Text(), nullable=False, server_default="schedule"),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
        sa.Column("duration_ms", sa.Integer()),
        sa.Column("file_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("bytes_written", sa.BigInteger(), nullable=False, server_default="0"),
        sa.Column("host", sa.Text()),
        sa.Column("git_sha", sa.String(40)),
        sa.UniqueConstraint(
            "source_id", "logical_date", "attempt", name="runs_source_date_attempt"
        ),
    )

    # THE idempotency guard. Enforced by the database, not by application logic,
    # so two concurrent runs of the same logical_date cannot both succeed.
    op.create_index(
        "runs_one_success",
        "runs",
        ["source_id", "logical_date"],
        unique=True,
        postgresql_where=sa.text("status = 'success'"),
    )
    op.create_index("runs_recent", "runs", ["source_id", sa.text("started_at DESC")])
    op.create_index(
        "runs_failed",
        "runs",
        ["status", sa.text("started_at DESC")],
        postgresql_where=sa.text("status = 'failed'"),
    )

    op.create_table(
        "run_errors",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "occurred_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column("error_class", sa.Text(), nullable=False),
        sa.Column("error_message", sa.Text(), nullable=False),
        sa.Column("http_status", sa.Integer()),
        sa.Column("url", sa.Text()),
        sa.Column("traceback", sa.Text()),
        sa.Column("is_transient", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.create_index("run_errors_class", "run_errors", ["error_class", sa.text("occurred_at DESC")])

    op.create_table(
        "files",
        sa.Column("file_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id",
            sa.Text(),
            sa.ForeignKey("sources.source_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("size_bytes", sa.BigInteger(), nullable=False),
        sa.Column("content_type", sa.Text()),
        sa.Column("extension", sa.Text()),
        # relative to LAKE_NAS_ROOT so the lake can be remounted anywhere
        sa.Column("nas_path", sa.Text(), nullable=False, unique=True),
        sa.Column("layer", sa.Text(), nullable=False, server_default="raw"),
        sa.Column(
            "first_seen_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("archived_at", sa.DateTime(timezone=True)),
        sa.Column("deleted_at", sa.DateTime(timezone=True)),
        sa.UniqueConstraint("source_id", "sha256", name="files_source_sha"),
    )
    op.create_index("files_sha", "files", ["sha256"])
    op.create_index(
        "files_live",
        "files",
        ["source_id", "layer"],
        postgresql_where=sa.text("deleted_at IS NULL"),
    )

    # was_new is the point of this table: it separates "the source stopped
    # publishing" from "our scraper broke". Different alerts, different fixes.
    op.create_table(
        "file_observations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.file_id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("was_new", sa.Boolean(), nullable=False),
        sa.Column("url", sa.Text()),
        sa.Column("http_status", sa.Integer()),
        sa.Column("etag", sa.Text()),
        sa.Column("last_modified", sa.Text()),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("run_id", "file_id", name="file_obs_run_file"),
    )
    op.create_index(
        "file_obs_recent", "file_observations", ["file_id", sa.text("observed_at DESC")]
    )

    op.create_table(
        "validations",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column(
            "run_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("runs.run_id", ondelete="CASCADE"),
        ),
        sa.Column(
            "file_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("files.file_id", ondelete="CASCADE"),
        ),
        sa.Column("check_name", sa.Text(), nullable=False),
        sa.Column("passed", sa.Boolean(), nullable=False),
        sa.Column("rows_total", sa.BigInteger()),
        sa.Column("rows_rejected", sa.BigInteger()),
        sa.Column("detail", postgresql.JSONB()),
        sa.Column(
            "checked_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.create_table(
        "datasets",
        sa.Column("dataset_id", sa.Text(), primary_key=True),
        sa.Column("source_id", sa.Text(), sa.ForeignKey("sources.source_id")),
        sa.Column("nas_path", sa.Text(), nullable=False),
        sa.Column("format", sa.Text(), nullable=False, server_default="parquet"),
        sa.Column("partition_keys", postgresql.ARRAY(sa.Text())),
        sa.Column("row_count", sa.BigInteger()),
        # lineage: which run produced these bytes, and via which git_sha
        sa.Column("built_from_run", postgresql.UUID(as_uuid=True), sa.ForeignKey("runs.run_id")),
        sa.Column(
            "built_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )

    op.execute(V_FRESHNESS)


def downgrade() -> None:
    op.execute("DROP VIEW IF EXISTS v_freshness")
    op.drop_table("datasets")
    op.drop_table("validations")
    op.drop_table("file_observations")
    op.drop_table("files")
    op.drop_table("run_errors")
    op.drop_table("runs")
    op.drop_table("sources")
    op.execute("DROP TYPE IF EXISTS run_status")
    op.execute("DROP TYPE IF EXISTS schedule_kind")
