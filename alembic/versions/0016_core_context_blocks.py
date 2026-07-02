"""core context blocks

Revision ID: 0016_core_context_blocks
Revises: 0015_agent_graph_vector_policy
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0016_core_context_blocks"
down_revision = "0015_agent_graph_vector_policy"
branch_labels = None
depends_on = None


def _json() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def _has_column(table: str, column: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return column in {item["name"] for item in inspector.get_columns(table)}


def _has_index(table: str, index: str) -> bool:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if table not in inspector.get_table_names():
        return False
    return index in {item["name"] for item in inspector.get_indexes(table)}


def upgrade() -> None:
    bind = op.get_bind()
    if _has_index("memories", "ix_memories_source_file_marker"):
        op.drop_index("ix_memories_source_file_marker", table_name="memories")
    if _has_column("memories", "source_file_marker"):
        op.drop_column("memories", "source_file_marker")
    op.create_table(
        "core_context_blocks",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()") if bind.dialect.name == "postgresql" else None),
        sa.Column("block_key", sa.String(64), nullable=False),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active"),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("source", sa.String(64), nullable=False, server_default="system"),
        sa.Column("metadata", _json(), nullable=False, server_default=_json_default()),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("block_key", name="uq_core_context_blocks_block_key"),
    )
    for column in ("block_key", "status", "source"):
        op.create_index(f"ix_core_context_blocks_{column}", "core_context_blocks", [column], unique=column == "block_key")


def downgrade() -> None:
    op.drop_table("core_context_blocks")
    if not _has_column("memories", "source_file_marker"):
        op.add_column("memories", sa.Column("source_file_marker", sa.String(256), nullable=False, server_default=""))
    if not _has_index("memories", "ix_memories_source_file_marker"):
        op.create_index("ix_memories_source_file_marker", "memories", ["source_file_marker"])
