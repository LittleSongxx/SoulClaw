"""mcp runtime state

Revision ID: 0004_mcp_runtime_state
Revises: 0003_cron_runtime_state
Create Date: 2026-06-25
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0004_mcp_runtime_state"
down_revision = "0003_cron_runtime_state"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.add_column("mcp_servers", sa.Column("status", sa.String(32), nullable=False, server_default="disabled"))
    op.add_column("mcp_servers", sa.Column("last_connected_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("mcp_servers", sa.Column("last_error", sa.Text(), nullable=False, server_default=""))
    op.add_column("mcp_servers", sa.Column("tool_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("mcp_servers", sa.Column("tools_cache", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.create_index("ix_mcp_servers_status", "mcp_servers", ["status"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_status", table_name="mcp_servers")
    op.drop_column("mcp_servers", "tools_cache")
    op.drop_column("mcp_servers", "tool_count")
    op.drop_column("mcp_servers", "last_error")
    op.drop_column("mcp_servers", "last_connected_at")
    op.drop_column("mcp_servers", "status")
