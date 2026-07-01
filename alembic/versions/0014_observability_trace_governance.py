"""observability trace governance

Revision ID: 0014_observability_trace_governance
Revises: 0013_reliability_governance
Create Date: 2026-06-30
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "0014_observability_trace_governance"
down_revision = "0013_reliability_governance"
branch_labels = None
depends_on = None


TRACE_COLUMNS = (
    ("trace_id", sa.String(128)),
    ("request_id", sa.String(128)),
)


def upgrade() -> None:
    for table in ("runtime_events", "audit_events", "background_jobs", "outbox_messages"):
        for name, column_type in TRACE_COLUMNS:
            op.add_column(table, sa.Column(name, column_type, nullable=False, server_default=""))
            op.create_index(f"ix_{table}_{name}", table, [name])


def downgrade() -> None:
    for table in ("outbox_messages", "background_jobs", "audit_events", "runtime_events"):
        for name, _column_type in reversed(TRACE_COLUMNS):
            op.drop_index(f"ix_{table}_{name}", table_name=table)
            op.drop_column(table, name)
