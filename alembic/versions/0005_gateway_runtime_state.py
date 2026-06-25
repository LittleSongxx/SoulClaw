"""gateway runtime state

Revision ID: 0005_gateway_runtime_state
Revises: 0004_mcp_runtime_state
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

revision = "0005_gateway_runtime_state"
down_revision = "0004_mcp_runtime_state"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("gateway_connections", sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("gateway_connections", sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("gateway_connections", sa.Column("last_error", sa.Text(), nullable=False, server_default=""))
    op.add_column("gateway_connections", sa.Column("inbound_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("gateway_connections", sa.Column("outbound_count", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("gateway_connections", sa.Column("failure_count", sa.Integer(), nullable=False, server_default="0"))


def downgrade() -> None:
    op.drop_column("gateway_connections", "failure_count")
    op.drop_column("gateway_connections", "outbound_count")
    op.drop_column("gateway_connections", "inbound_count")
    op.drop_column("gateway_connections", "last_error")
    op.drop_column("gateway_connections", "last_outbound_at")
    op.drop_column("gateway_connections", "last_inbound_at")
