"""admin panel: users, sessions, audit log

Revision ID: 0002
Revises: 0001
Create Date: 2026-07-11

Three tables that exist only so the admin panel can exist. The pipeline neither
reads nor writes any of them — a lake with no users still scrapes, transforms,
and serves.

`audit_log` is the one that earns its keep. The panel can edit
configs/sources.yaml, a git-tracked file that otherwise changes only through
review; this table is what replaces the commit, recording the actor and the full
previous content. A bad edit at 3am with no diff and no author is exactly the
failure mode this project exists to avoid.
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op

revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "users",
        sa.Column("user_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.Text, nullable=False, unique=True),
        sa.Column("display_name", sa.Text, nullable=False),
        # Argon2id. The plaintext is never stored, never logged, and never leaves
        # the request that carried it.
        sa.Column("password_hash", sa.Text, nullable=False),
        # A disabled user keeps their row, so the audit log still resolves their
        # name — but cannot log in, and their sessions are revoked.
        sa.Column("is_active", sa.Boolean, nullable=False, server_default=sa.text("true")),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("last_login_at", sa.DateTime(timezone=True)),
    )

    op.create_table(
        "user_sessions",
        sa.Column("session_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="CASCADE"),
            nullable=False,
        ),
        # sha256 of the token in the cookie — never the token itself. A dump of
        # this table hands an attacker nothing they can replay.
        sa.Column("token_hash", sa.String(64), nullable=False, unique=True),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("user_agent", sa.Text),
        sa.Column("ip", sa.Text),
    )
    # The sweeper deletes expired sessions; it should not seq-scan to find them.
    op.create_index("ix_user_sessions_expires_at", "user_sessions", ["expires_at"])

    op.create_table(
        "audit_log",
        sa.Column("entry_id", sa.dialects.postgresql.UUID(as_uuid=True), primary_key=True),
        # SET NULL, not CASCADE: the log outlives the account. Deleting a user
        # must never erase what they did.
        sa.Column(
            "user_id",
            sa.dialects.postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.user_id", ondelete="SET NULL"),
        ),
        # Denormalised on purpose — survives the user row being deleted.
        sa.Column("actor_email", sa.Text, nullable=False),
        sa.Column("action", sa.Text, nullable=False),
        sa.Column("target", sa.Text),
        sa.Column(
            "detail",
            sa.dialects.postgresql.JSONB,
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "occurred_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
    )
    op.create_index("ix_audit_log_occurred_at", "audit_log", [sa.text("occurred_at DESC")])


def downgrade() -> None:
    op.drop_table("audit_log")
    op.drop_table("user_sessions")
    op.drop_table("users")
