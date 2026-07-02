"""memory governance skill wiki quality

Revision ID: 0012_memory_governance_skill_wiki_quality
Revises: 0011_wiki_fts
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "0012_memory_governance_skill_wiki_quality"
down_revision = "0011_wiki_fts"
branch_labels = None
depends_on = None


def _jsonb() -> sa.types.TypeEngine:
    return postgresql.JSONB(astext_type=sa.Text()).with_variant(sa.JSON(), "sqlite")


def _uuid() -> sa.types.TypeEngine:
    return postgresql.UUID(as_uuid=True).with_variant(sa.String(32), "sqlite")


def _json_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'{}'::jsonb")
    return sa.text("'{}'")


def _json_list_default() -> sa.TextClause:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        return sa.text("'[]'::jsonb")
    return sa.text("'[]'")


def upgrade() -> None:
    bind = op.get_bind()

    op.add_column("wiki_pages", sa.Column("claims", _jsonb(), nullable=False, server_default=_json_list_default()))
    op.add_column("wiki_pages", sa.Column("source_refs", _jsonb(), nullable=False, server_default=_json_list_default()))
    op.add_column("wiki_pages", sa.Column("stale_after", sa.DateTime(timezone=True), nullable=True))
    op.create_index("ix_wiki_pages_stale_after", "wiki_pages", ["stale_after"])

    op.create_table(
        "memory_history",
        sa.Column("id", _uuid(), primary_key=True),
        sa.Column("memory_id", _uuid(), nullable=True, index=True),
        sa.Column("action", sa.String(64), nullable=False, index=True),
        sa.Column("before_snapshot", _jsonb(), nullable=False, server_default=_json_default()),
        sa.Column("after_snapshot", _jsonb(), nullable=False, server_default=_json_default()),
        sa.Column("actor", sa.String(80), nullable=False, server_default="system", index=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now(), index=True),
    )

    op.add_column("evolution_proposals", sa.Column("target_checksum", sa.String(128), nullable=False, server_default=""))
    op.add_column("evolution_proposals", sa.Column("stale_reason", sa.Text(), nullable=False, server_default=""))
    op.create_index("ix_evolution_proposals_target_checksum", "evolution_proposals", ["target_checksum"])

    if bind.dialect.name == "postgresql":
        op.execute(
            """
            CREATE INDEX IF NOT EXISTS ix_memories_fts
            ON memories
            USING GIN ((
                setweight(to_tsvector('simple', coalesce(kind, '')), 'A') ||
                setweight(to_tsvector('simple', coalesce(content, '')), 'B') ||
                setweight(to_tsvector('simple', coalesce(source, '')), 'C')
            ))
            """
        )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name == "postgresql":
        op.execute("DROP INDEX IF EXISTS ix_memories_fts")

    op.drop_index("ix_evolution_proposals_target_checksum", table_name="evolution_proposals")
    op.drop_column("evolution_proposals", "stale_reason")
    op.drop_column("evolution_proposals", "target_checksum")

    op.drop_table("memory_history")

    op.drop_index("ix_wiki_pages_stale_after", table_name="wiki_pages")
    op.drop_column("wiki_pages", "stale_after")
    op.drop_column("wiki_pages", "source_refs")
    op.drop_column("wiki_pages", "claims")
