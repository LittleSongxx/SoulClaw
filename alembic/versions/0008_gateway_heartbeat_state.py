"""gateway heartbeat state

Revision ID: 0008_gateway_heartbeat_state
Revises: 0007_background_jobs
Create Date: 2026-06-26
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0008_gateway_heartbeat_state"
down_revision = "0007_background_jobs"
branch_labels = None
depends_on = None


def _json_list() -> sa.types.TypeEngine:
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    op.add_column("gateway_connections", sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("gateway_connections", sa.Column("instance_id", sa.String(128), nullable=False, server_default=""))
    op.add_column("gateway_connections", sa.Column("version", sa.String(80), nullable=False, server_default=""))
    op.add_column("gateway_connections", sa.Column("capabilities", _json_list(), nullable=False, server_default=sa.text("'[]'")))


def downgrade() -> None:
    op.drop_column("gateway_connections", "capabilities")
    op.drop_column("gateway_connections", "version")
    op.drop_column("gateway_connections", "instance_id")
    op.drop_column("gateway_connections", "last_heartbeat_at")
