"""a2a runtime state

Revision ID: 0009_a2a_runtime_state
Revises: 0008_gateway_heartbeat_state
Create Date: 2026-06-28
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0009_a2a_runtime_state"
down_revision = "0008_gateway_heartbeat_state"
branch_labels = None
depends_on = None


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _jsonb_list() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def upgrade() -> None:
    op.create_table(
        "a2a_agent_connections",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False),
        sa.Column("kind", sa.String(64), nullable=False, server_default="a2a"),
        sa.Column("endpoint", sa.Text(), nullable=False, server_default=""),
        sa.Column("rpc_url", sa.Text(), nullable=False, server_default=""),
        sa.Column("agent_card", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("config", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("capabilities", _jsonb_list(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("skills", _jsonb_list(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("last_discovered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_a2a_agent_connections_name", "a2a_agent_connections", ["name"], unique=True)
    op.create_index("ix_a2a_agent_connections_kind", "a2a_agent_connections", ["kind"])
    op.create_index("ix_a2a_agent_connections_enabled", "a2a_agent_connections", ["enabled"])
    op.create_index("ix_a2a_agent_connections_status", "a2a_agent_connections", ["status"])

    op.create_table(
        "a2a_tasks",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("connection_name", sa.String(128), nullable=False, server_default=""),
        sa.Column("capability", sa.String(128), nullable=False, server_default=""),
        sa.Column("context_id", sa.String(128), nullable=False, server_default=""),
        sa.Column("remote_task_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("remote_context_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="submitted"),
        sa.Column("input_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_a2a_tasks_task_id", "a2a_tasks", ["task_id"], unique=True)
    op.create_index("ix_a2a_tasks_connection_name", "a2a_tasks", ["connection_name"])
    op.create_index("ix_a2a_tasks_capability", "a2a_tasks", ["capability"])
    op.create_index("ix_a2a_tasks_context_id", "a2a_tasks", ["context_id"])
    op.create_index("ix_a2a_tasks_remote_task_id", "a2a_tasks", ["remote_task_id"])
    op.create_index("ix_a2a_tasks_remote_context_id", "a2a_tasks", ["remote_context_id"])
    op.create_index("ix_a2a_tasks_status", "a2a_tasks", ["status"])

    op.create_table(
        "a2a_artifacts",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("artifact_id", sa.String(128), nullable=False),
        sa.Column("name", sa.String(256), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(128), nullable=False, server_default="text/plain"),
        sa.Column("uri", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("parts", _jsonb_list(), nullable=False, server_default=sa.text("'[]'::jsonb")),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_a2a_artifacts_task_id", "a2a_artifacts", ["task_id"])
    op.create_index("ix_a2a_artifacts_artifact_id", "a2a_artifacts", ["artifact_id"])
    op.create_index("ix_a2a_artifacts_created_at", "a2a_artifacts", ["created_at"])

    op.create_table(
        "a2a_events",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("task_id", sa.String(128), nullable=False),
        sa.Column("event_type", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_index("ix_a2a_events_task_id", "a2a_events", ["task_id"])
    op.create_index("ix_a2a_events_event_type", "a2a_events", ["event_type"])
    op.create_index("ix_a2a_events_created_at", "a2a_events", ["created_at"])


def downgrade() -> None:
    op.drop_index("ix_a2a_events_created_at", table_name="a2a_events")
    op.drop_index("ix_a2a_events_event_type", table_name="a2a_events")
    op.drop_index("ix_a2a_events_task_id", table_name="a2a_events")
    op.drop_table("a2a_events")

    op.drop_index("ix_a2a_artifacts_created_at", table_name="a2a_artifacts")
    op.drop_index("ix_a2a_artifacts_artifact_id", table_name="a2a_artifacts")
    op.drop_index("ix_a2a_artifacts_task_id", table_name="a2a_artifacts")
    op.drop_table("a2a_artifacts")

    op.drop_index("ix_a2a_tasks_status", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_remote_context_id", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_remote_task_id", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_context_id", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_capability", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_connection_name", table_name="a2a_tasks")
    op.drop_index("ix_a2a_tasks_task_id", table_name="a2a_tasks")
    op.drop_table("a2a_tasks")

    op.drop_index("ix_a2a_agent_connections_status", table_name="a2a_agent_connections")
    op.drop_index("ix_a2a_agent_connections_enabled", table_name="a2a_agent_connections")
    op.drop_index("ix_a2a_agent_connections_kind", table_name="a2a_agent_connections")
    op.drop_index("ix_a2a_agent_connections_name", table_name="a2a_agent_connections")
    op.drop_table("a2a_agent_connections")
