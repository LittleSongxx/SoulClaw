"""closed loop controls

Revision ID: 0010_closed_loop_controls
Revises: 0009_a2a_runtime_state
Create Date: 2026-06-29
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0010_closed_loop_controls"
down_revision = "0009_a2a_runtime_state"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def upgrade() -> None:
    op.add_column("wiki_error_book", sa.Column("constraint_rule", sa.Text(), nullable=False, server_default=""))
    op.add_column("wiki_error_book", sa.Column("verification_method", sa.Text(), nullable=False, server_default=""))
    op.add_column("wiki_error_book", sa.Column("source_refs", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("wiki_error_book", sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="open"))
    op.create_index("ix_wiki_error_book_lifecycle_status", "wiki_error_book", ["lifecycle_status"])

    op.add_column("memories", sa.Column("status", sa.String(32), nullable=False, server_default="active"))
    op.add_column("memories", sa.Column("superseded_by", _uuid(), nullable=True))
    op.add_column("memories", sa.Column("source_file_marker", sa.String(256), nullable=False, server_default=""))
    op.add_column("memories", sa.Column("valid_from", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("valid_to", sa.DateTime(timezone=True), nullable=True))
    op.add_column("memories", sa.Column("provenance", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.create_index("ix_memories_status", "memories", ["status"])
    op.create_index("ix_memories_superseded_by", "memories", ["superseded_by"])
    op.create_index("ix_memories_source_file_marker", "memories", ["source_file_marker"])

    op.add_column("approvals", sa.Column("turn_checkpoint", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("approvals", sa.Column("original_tool_call", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("approvals", sa.Column("allowed_decisions", _jsonb(), nullable=False, server_default=sa.text("'[]'::jsonb")))
    op.add_column("approvals", sa.Column("edited_arguments", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("approvals", sa.Column("resume_state", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))

    op.add_column("mcp_servers", sa.Column("config_source", sa.String(256), nullable=False, server_default="manual"))
    op.add_column("mcp_servers", sa.Column("permission_policy", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")))
    op.add_column("mcp_servers", sa.Column("session_mode", sa.String(32), nullable=False, server_default="transient"))
    op.add_column("mcp_servers", sa.Column("health_status", sa.String(32), nullable=False, server_default="unknown"))
    op.add_column("mcp_servers", sa.Column("last_imported_checksum", sa.String(128), nullable=False, server_default=""))
    op.create_index("ix_mcp_servers_config_source", "mcp_servers", ["config_source"])
    op.create_index("ix_mcp_servers_session_mode", "mcp_servers", ["session_mode"])
    op.create_index("ix_mcp_servers_health_status", "mcp_servers", ["health_status"])


def downgrade() -> None:
    op.drop_index("ix_mcp_servers_health_status", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_session_mode", table_name="mcp_servers")
    op.drop_index("ix_mcp_servers_config_source", table_name="mcp_servers")
    op.drop_column("mcp_servers", "last_imported_checksum")
    op.drop_column("mcp_servers", "health_status")
    op.drop_column("mcp_servers", "session_mode")
    op.drop_column("mcp_servers", "permission_policy")
    op.drop_column("mcp_servers", "config_source")

    op.drop_column("approvals", "resume_state")
    op.drop_column("approvals", "edited_arguments")
    op.drop_column("approvals", "allowed_decisions")
    op.drop_column("approvals", "original_tool_call")
    op.drop_column("approvals", "turn_checkpoint")

    op.drop_index("ix_memories_source_file_marker", table_name="memories")
    op.drop_index("ix_memories_superseded_by", table_name="memories")
    op.drop_index("ix_memories_status", table_name="memories")
    op.drop_column("memories", "provenance")
    op.drop_column("memories", "valid_to")
    op.drop_column("memories", "valid_from")
    op.drop_column("memories", "source_file_marker")
    op.drop_column("memories", "superseded_by")
    op.drop_column("memories", "status")

    op.drop_index("ix_wiki_error_book_lifecycle_status", table_name="wiki_error_book")
    op.drop_column("wiki_error_book", "lifecycle_status")
    op.drop_column("wiki_error_book", "source_refs")
    op.drop_column("wiki_error_book", "verification_method")
    op.drop_column("wiki_error_book", "constraint_rule")
