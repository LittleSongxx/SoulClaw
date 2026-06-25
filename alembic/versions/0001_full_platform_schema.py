"""full platform schema

Revision ID: 0001_full_platform_schema
Revises:
Create Date: 2026-06-25
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision = "0001_full_platform_schema"
down_revision = None
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS pgcrypto")

    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("username", sa.String(80), nullable=False, unique=True, index=True),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role", sa.String(32), nullable=False, server_default="admin"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.text("true")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "sessions",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("channel", sa.String(64), nullable=False, server_default="local"),
        sa.Column("external_user_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("title", sa.String(256), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "audit_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("actor_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("action", sa.String(128), nullable=False, index=True),
        sa.Column("target_type", sa.String(64), nullable=False, index=True),
        sa.Column("target_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "runtime_events",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("event_type", sa.String(128), nullable=False, index=True),
        sa.Column("severity", sa.String(16), nullable=False, server_default="info"),
        sa.Column("session_id", sa.String(256), nullable=False, server_default="", index=True),
        sa.Column("turn_id", sa.String(256), nullable=False, server_default="", index=True),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "wiki_sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("source_key", sa.String(512), nullable=False, unique=True, index=True),
        sa.Column("kind", sa.String(64), nullable=False, server_default="markdown"),
        sa.Column("uri", sa.Text(), nullable=False, server_default=""),
        sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "wiki_pages",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("page_key", sa.String(512), nullable=False, unique=True, index=True),
        sa.Column("title", sa.String(512), nullable=False, index=True),
        sa.Column("page_type", sa.String(64), nullable=False, server_default="note", index=True),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False, server_default=""),
        sa.Column("body", sa.Text(), nullable=False, server_default=""),
        sa.Column("aliases", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("tags", postgresql.ARRAY(sa.Text()), nullable=False, server_default=sa.text("ARRAY[]::text[]")),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "wiki_links",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("src_page_key", sa.String(512), nullable=False, index=True),
        sa.Column("dst_page_key", sa.String(512), nullable=False, index=True),
        sa.Column("link_type", sa.String(64), nullable=False, server_default="wikilink"),
        sa.Column("status", sa.String(32), nullable=False, server_default="unresolved", index=True),
        sa.Column("anchor_text", sa.String(512), nullable=False, server_default=""),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "wiki_error_book",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("error_type", sa.String(80), nullable=False, index=True),
        sa.Column("page_key", sa.String(512), nullable=False, server_default="", index=True),
        sa.Column("root_cause", sa.Text(), nullable=False, server_default=""),
        sa.Column("constraint", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="open", index=True),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("fixed_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "wiki_compile_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("status", sa.String(32), nullable=False, server_default="running", index=True),
        sa.Column("pages_seen", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("pages_indexed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("errors", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("kind", sa.String(32), nullable=False, index=True),
        sa.Column("content", sa.Text(), nullable=False),
        sa.Column("source", sa.String(32), nullable=False, server_default="explicit", index=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false"), index=True),
        sa.Column("archived", sa.Boolean(), nullable=False, server_default=sa.text("false"), index=True),
        sa.Column("importance", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("stability", sa.Float(), nullable=False, server_default="0.5"),
        sa.Column("supersedes_id", postgresql.UUID(as_uuid=True), nullable=True, index=True),
        sa.Column("source_turn_id", sa.String(256), nullable=False, server_default="", index=True),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_verified_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "memory_conflicts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("left_memory_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("right_memory_id", postgresql.UUID(as_uuid=True), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="open", index=True),
        sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "memory_probes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("expected", sa.Text(), nullable=False, server_default=""),
        sa.Column("status", sa.String(32), nullable=False, server_default="active", index=True),
        sa.Column("last_result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "skills",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("skill_key", sa.String(256), nullable=False, unique=True, index=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("description", sa.Text(), nullable=False, server_default=""),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("status", sa.String(32), nullable=False, server_default="active", index=True),
        sa.Column("pinned", sa.Boolean(), nullable=False, server_default=sa.text("false")),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "skill_files",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("skill_key", sa.String(256), nullable=False, index=True),
        sa.Column("file_path", sa.Text(), nullable=False),
        sa.Column("checksum", sa.String(128), nullable=False, server_default=""),
        sa.Column("content", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "skill_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("skill_key", sa.String(256), nullable=False, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("before_snapshot", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after_snapshot", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("actor", sa.String(80), nullable=False, server_default="system"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )
    op.create_table(
        "skill_tests",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("skill_key", sa.String(256), nullable=False, index=True),
        sa.Column("name", sa.String(256), nullable=False),
        sa.Column("spec", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("last_status", sa.String(32), nullable=False, server_default="never_run", index=True),
        sa.Column("last_result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "evolution_proposals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("target_type", sa.String(64), nullable=False, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", index=True),
        sa.Column("risk_level", sa.String(32), nullable=False, server_default="medium", index=True),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("evidence", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("before_snapshot", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("after_snapshot", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "tool_runs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("turn_id", sa.String(256), nullable=False, server_default="", index=True),
        sa.Column("tool_name", sa.String(128), nullable=False, index=True),
        sa.Column("status", sa.String(32), nullable=False, server_default="running", index=True),
        sa.Column("arguments", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("result", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "approvals",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("status", sa.String(32), nullable=False, server_default="pending", index=True),
        sa.Column("subject_type", sa.String(64), nullable=False, index=True),
        sa.Column("subject_id", sa.String(256), nullable=False, server_default=""),
        sa.Column("payload", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
    )
    op.create_table(
        "cron_jobs",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("cron_expr", sa.String(80), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False, server_default="Asia/Hong_Kong"),
        sa.Column("instruction", sa.Text(), nullable=False, server_default=""),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"), index=True),
        sa.Column("metadata", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )
    op.create_table(
        "mcp_servers",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True, server_default=sa.text("gen_random_uuid()")),
        sa.Column("name", sa.String(128), nullable=False, unique=True, index=True),
        sa.Column("transport", sa.String(32), nullable=False, server_default="stdio"),
        sa.Column("command", sa.Text(), nullable=False, server_default=""),
        sa.Column("url", sa.Text(), nullable=False, server_default=""),
        sa.Column("config", _jsonb(), nullable=False, server_default=sa.text("'{}'::jsonb")),
        sa.Column("enabled", sa.Boolean(), nullable=False, server_default=sa.text("true"), index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()),
    )


def downgrade() -> None:
    for table in (
        "mcp_servers",
        "cron_jobs",
        "approvals",
        "tool_runs",
        "evolution_proposals",
        "skill_tests",
        "skill_history",
        "skill_files",
        "skills",
        "memory_probes",
        "memory_conflicts",
        "memories",
        "wiki_compile_runs",
        "wiki_error_book",
        "wiki_links",
        "wiki_pages",
        "wiki_sources",
        "runtime_events",
        "audit_events",
        "sessions",
        "users",
    ):
        op.drop_table(table)
