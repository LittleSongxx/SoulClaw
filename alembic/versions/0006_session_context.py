"""session context

Revision ID: 0006_session_context
Revises: 0005_gateway_runtime_state
Create Date: 2026-06-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0006_session_context"
down_revision = "0005_gateway_runtime_state"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.create_table(
        "session_messages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("turn_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("role", sa.String(32), nullable=False),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_session_messages_session_id", "session_messages", ["session_id"])
    op.create_index("ix_session_messages_turn_id", "session_messages", ["turn_id"])
    op.create_index("ix_session_messages_role", "session_messages", ["role"])
    op.create_index("ix_session_messages_created_at", "session_messages", ["created_at"])
    op.create_table(
        "session_summaries",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("session_id", sa.String(256), nullable=False, unique=True),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("summarized_message_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_session_summaries_session_id", "session_summaries", ["session_id"])


def downgrade() -> None:
    op.drop_index("ix_session_summaries_session_id", table_name="session_summaries")
    op.drop_table("session_summaries")
    op.drop_index("ix_session_messages_created_at", table_name="session_messages")
    op.drop_index("ix_session_messages_role", table_name="session_messages")
    op.drop_index("ix_session_messages_turn_id", table_name="session_messages")
    op.drop_index("ix_session_messages_session_id", table_name="session_messages")
    op.drop_table("session_messages")
