"""agent graph vector policy

Revision ID: 0015_agent_graph_vector_policy
Revises: 0014_observability_trace_governance
Create Date: 2026-07-02
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0015_agent_graph_vector_policy"
down_revision = "0014_observability_trace_governance"
branch_labels = None
depends_on = None


def _json() -> sa.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _uuid() -> sa.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    op.create_table(
        "agent_runs",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()") if bind.dialect.name == "postgresql" else None),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("thread_id", sa.String(256), nullable=False),
        sa.Column("session_id", sa.String(256), nullable=False),
        sa.Column("turn_id", sa.String(256), nullable=False),
        sa.Column("engine", sa.String(64), nullable=False, server_default="langgraph"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("route", sa.String(64), nullable=False, server_default=""),
        sa.Column("input_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("answer_preview", sa.Text(), nullable=False, server_default=""),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("state", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("metadata", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("run_id", "thread_id", "session_id", "turn_id", "engine", "status", "route", "started_at"):
        op.create_index(f"ix_agent_runs_{column}", "agent_runs", [column], unique=column == "run_id")

    op.create_table(
        "agent_run_steps",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()") if bind.dialect.name == "postgresql" else None),
        sa.Column("run_id", sa.String(128), nullable=False),
        sa.Column("step_name", sa.String(128), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("status", sa.String(32), nullable=False, server_default="running"),
        sa.Column("input", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("output", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    for column in ("run_id", "step_name", "sequence", "status", "started_at"):
        op.create_index(f"ix_agent_run_steps_{column}", "agent_run_steps", [column])

    op.create_table(
        "knowledge_embeddings",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()") if bind.dialect.name == "postgresql" else None),
        sa.Column("source_type", sa.String(64), nullable=False),
        sa.Column("source_id", sa.String(512), nullable=False),
        sa.Column("chunk_key", sa.String(512), nullable=False, server_default=""),
        sa.Column("title", sa.Text(), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("content_hash", sa.String(128), nullable=False),
        sa.Column("embedding", _json(), nullable=False, server_default=sa.text("'[]'") if bind.dialect.name != "postgresql" else sa.text("'[]'::jsonb")),
        sa.Column("vector_status", sa.String(32), nullable=False, server_default="pending"),
        sa.Column("model", sa.String(128), nullable=False, server_default=""),
        sa.Column("dimensions", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("stale", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("last_embedded_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("error", sa.Text(), nullable=False, server_default=""),
        sa.UniqueConstraint("source_type", "source_id", "chunk_key", name="uq_knowledge_embedding_source_chunk"),
    )
    for column in ("source_type", "source_id", "chunk_key", "content_hash", "vector_status", "model", "dimensions", "stale"):
        op.create_index(f"ix_knowledge_embeddings_{column}", "knowledge_embeddings", [column])
    if bind.dialect.name == "postgresql":
        op.execute("ALTER TABLE knowledge_embeddings ADD COLUMN IF NOT EXISTS vector_embedding vector(1536)")
        op.execute(
            "CREATE INDEX IF NOT EXISTS ix_knowledge_embeddings_vector_hnsw "
            "ON knowledge_embeddings USING hnsw (vector_embedding vector_cosine_ops)"
        )

    op.create_table(
        "policy_rules",
        sa.Column("id", _uuid(), primary_key=True, server_default=sa.text("gen_random_uuid()") if bind.dialect.name == "postgresql" else None),
        sa.Column("rule_id", sa.String(128), nullable=False),
        sa.Column("subject", sa.String(128), nullable=False),
        sa.Column("scope", sa.String(128), nullable=False),
        sa.Column("action", sa.String(32), nullable=False, server_default="allow"),
        sa.Column("risk_level", sa.String(32), nullable=False, server_default="low"),
        sa.Column("requires_approval", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("config", _json(), nullable=False, server_default=sa.text("'{}'") if bind.dialect.name != "postgresql" else sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("rule_id", name="uq_policy_rules_rule_id"),
    )
    for column in ("rule_id", "subject", "scope", "action", "risk_level", "requires_approval", "enabled"):
        op.create_index(f"ix_policy_rules_{column}", "policy_rules", [column], unique=column == "rule_id")


def downgrade() -> None:
    bind = op.get_bind()
    op.drop_table("policy_rules")
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_knowledge_embeddings_vector_hnsw")
    op.drop_table("knowledge_embeddings")
    op.drop_table("agent_run_steps")
    op.drop_table("agent_runs")
