"""background jobs

Revision ID: 0007_background_jobs
Revises: 0006_session_context
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0007_background_jobs"
down_revision = "0006_session_context"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "background_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_name", sa.String(128), nullable=False),
        sa.Column("queue_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("triggered_by", sa.String(128), nullable=False, server_default=""),
        sa.Column("cron_job_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_index("ix_background_jobs_task_name", "background_jobs", ["task_name"])
    op.create_index("ix_background_jobs_queue_id", "background_jobs", ["queue_id"])
    op.create_index("ix_background_jobs_status", "background_jobs", ["status"])
    op.create_index("ix_background_jobs_cron_job_id", "background_jobs", ["cron_job_id"])
    op.create_index("ix_background_jobs_created_at", "background_jobs", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_background_jobs_created_at", table_name="background_jobs")
    op.drop_index("ix_background_jobs_cron_job_id", table_name="background_jobs")
    op.drop_index("ix_background_jobs_status", table_name="background_jobs")
    op.drop_index("ix_background_jobs_queue_id", table_name="background_jobs")
    op.drop_index("ix_background_jobs_task_name", table_name="background_jobs")
    op.drop_table("background_jobs")
