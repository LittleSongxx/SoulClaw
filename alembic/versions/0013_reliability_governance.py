"""reliability governance

Revision ID: 0013_reliability_governance
Revises: 0012_memory_governance_skill_wiki_quality
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0013_reliability_governance"
down_revision = "0012_memory_governance_skill_wiki_quality"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def upgrade() -> None:
    op.add_column("background_jobs", sa.Column("idempotency_key", sa.String(256), nullable=False, server_default=""))
    op.add_column("background_jobs", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("background_jobs", sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="3"))
    op.add_column("background_jobs", sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("background_jobs", sa.Column("locked_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("background_jobs", sa.Column("lock_owner", sa.String(128), nullable=False, server_default=""))
    op.add_column("background_jobs", sa.Column("dead_letter_reason", sa.Text(), nullable=False, server_default=""))
    op.create_index("ix_background_jobs_idempotency_key", "background_jobs", ["idempotency_key"])
    op.create_index("ix_background_jobs_next_retry_at", "background_jobs", ["next_retry_at"])

    op.add_column("cron_jobs", sa.Column("backoff_until", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cron_jobs", sa.Column("last_enqueue_key", sa.String(256), nullable=False, server_default=""))
    op.create_index("ix_cron_jobs_backoff_until", "cron_jobs", ["backoff_until"])
    op.create_index("ix_cron_jobs_last_enqueue_key", "cron_jobs", ["last_enqueue_key"])

    op.create_table(
        "outbox_messages",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("topic", sa.String(128), nullable=False, index=True),
        sa.Column("aggregate_type", sa.String(64), nullable=False, index=True),
        sa.Column("aggregate_id", sa.String(128), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, server_default="", index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", index=True),
        sa.Column("payload", _jsonb(), nullable=False, server_default=_json_default()),
        sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("dispatched_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("idempotency_key", name="uq_outbox_messages_idempotency_key"),
    )

    op.create_table(
        "idempotency_records",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("scope", sa.String(128), nullable=False, index=True),
        sa.Column("idempotency_key", sa.String(256), nullable=False, index=True),
        sa.Column("request_hash", sa.String(128), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="processing", index=True),
        sa.Column("response", _jsonb(), nullable=False, server_default=_json_default()),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True, index=True),
        sa.UniqueConstraint("scope", "idempotency_key", name="uq_idempotency_scope_key"),
    )


def downgrade() -> None:
    op.drop_table("idempotency_records")
    op.drop_table("outbox_messages")

    op.drop_index("ix_cron_jobs_last_enqueue_key", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_backoff_until", table_name="cron_jobs")
    op.drop_column("cron_jobs", "last_enqueue_key")
    op.drop_column("cron_jobs", "backoff_until")

    op.drop_index("ix_background_jobs_next_retry_at", table_name="background_jobs")
    op.drop_index("ix_background_jobs_idempotency_key", table_name="background_jobs")
    op.drop_column("background_jobs", "dead_letter_reason")
    op.drop_column("background_jobs", "lock_owner")
    op.drop_column("background_jobs", "locked_at")
    op.drop_column("background_jobs", "next_retry_at")
    op.drop_column("background_jobs", "max_attempts")
    op.drop_column("background_jobs", "attempt_count")
    op.drop_column("background_jobs", "idempotency_key")
