"""cron runtime state

Revision ID: 0003_cron_runtime_state
Revises: 0002_gateway_connections
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0003_cron_runtime_state"
down_revision = "0002_gateway_connections"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("cron_jobs", sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cron_jobs", sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("cron_jobs", sa.Column("last_status", sa.String(32), nullable=False, server_default="never_run"))
    op.add_column("cron_jobs", sa.Column("last_result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("cron_jobs", sa.Column("run_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("cron_jobs", sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_cron_jobs_next_run_at", "cron_jobs", ["next_run_at"])
    op.create_index("ix_cron_jobs_last_status", "cron_jobs", ["last_status"])


def downgrade() -> None:
    op.drop_index("ix_cron_jobs_last_status", table_name="cron_jobs")
    op.drop_index("ix_cron_jobs_next_run_at", table_name="cron_jobs")
    op.drop_column("cron_jobs", "failure_count")
    op.drop_column("cron_jobs", "run_count")
    op.drop_column("cron_jobs", "last_result")
    op.drop_column("cron_jobs", "last_status")
    op.drop_column("cron_jobs", "next_run_at")
    op.drop_column("cron_jobs", "last_run_at")
