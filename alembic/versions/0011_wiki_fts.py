"""wiki full text search

Revision ID: 0011_wiki_fts
Revises: 0010_closed_loop_controls
Create Date: 2026-06-30
"""

from __future__ import annotations

from alembic import op

revision = "0011_wiki_fts"
down_revision = "0010_closed_loop_controls"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN aliases DROP DEFAULT")
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN tags DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE wiki_pages
        ALTER COLUMN aliases TYPE jsonb USING to_jsonb(aliases),
        ALTER COLUMN tags TYPE jsonb USING to_jsonb(tags)
        """
    )
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN aliases SET DEFAULT '[]'::jsonb")
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN tags SET DEFAULT '[]'::jsonb")
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS ix_wiki_pages_fts
        ON wiki_pages
        USING GIN ((
            setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
            setweight(to_tsvector('simple', coalesce(summary, '')), 'B') ||
            setweight(
                jsonb_to_tsvector(
                    'simple',
                    CASE WHEN jsonb_typeof(aliases) = 'array' THEN aliases ELSE '[]'::jsonb END,
                    '["string"]'::jsonb
                ),
                'B'
            ) ||
            setweight(
                jsonb_to_tsvector(
                    'simple',
                    CASE WHEN jsonb_typeof(tags) = 'array' THEN tags ELSE '[]'::jsonb END,
                    '["string"]'::jsonb
                ),
                'B'
            ) ||
            setweight(to_tsvector('simple', coalesce(body, '')), 'C')
        ))
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_wiki_pages_fts")
    op.execute(
        """
        CREATE OR REPLACE FUNCTION soulclaw_jsonb_text_array(value jsonb)
        RETURNS text[]
        LANGUAGE sql
        IMMUTABLE
        AS $$
            SELECT COALESCE(array_agg(item), ARRAY[]::text[])
            FROM jsonb_array_elements_text(
                CASE WHEN jsonb_typeof(value) = 'array' THEN value ELSE '[]'::jsonb END
            ) AS item
        $$
        """
    )
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN aliases DROP DEFAULT")
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN tags DROP DEFAULT")
    op.execute(
        """
        ALTER TABLE wiki_pages
        ALTER COLUMN aliases TYPE text[] USING soulclaw_jsonb_text_array(aliases),
        ALTER COLUMN tags TYPE text[] USING soulclaw_jsonb_text_array(tags)
        """
    )
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN aliases SET DEFAULT ARRAY[]::text[]")
    op.execute("ALTER TABLE wiki_pages ALTER COLUMN tags SET DEFAULT ARRAY[]::text[]")
    op.execute("DROP FUNCTION IF EXISTS soulclaw_jsonb_text_array(jsonb)")
