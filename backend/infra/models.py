"""SQLAlchemy models for the v2 Postgres schema."""

from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, Integer, String, Text, func, text
from sqlalchemy.dialects.postgresql import ARRAY, JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
    )


def _uuid_pk() -> Any:
    return mapped_column(UUID(as_uuid=True), primary_key=True, server_default=text("gen_random_uuid()"))


def _json_default() -> Any:
    return mapped_column(JSONB, nullable=False, server_default=text("'{}'::jsonb"), default=dict)


class User(Base, TimestampMixin):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    username: Mapped[str] = mapped_column(String(80), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role: Mapped[str] = mapped_column(String(32), nullable=False, server_default="admin")
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"))


class Session(Base, TimestampMixin):
    __tablename__ = "sessions"

    id: Mapped[uuid.UUID] = _uuid_pk()
    user_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    channel: Mapped[str] = mapped_column(String(64), nullable=False, server_default="local")
    external_user_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    title: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    action: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    target_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    payload: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class RuntimeEvent(Base):
    __tablename__ = "runtime_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    event_type: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    severity: Mapped[str] = mapped_column(String(16), nullable=False, server_default="info")
    session_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="", index=True)
    turn_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="", index=True)
    payload: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class WikiSource(Base, TimestampMixin):
    __tablename__ = "wiki_sources"

    id: Mapped[uuid.UUID] = _uuid_pk()
    source_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, server_default="markdown")
    uri: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class WikiPage(Base, TimestampMixin):
    __tablename__ = "wiki_pages"

    id: Mapped[uuid.UUID] = _uuid_pk()
    page_key: Mapped[str] = mapped_column(String(512), nullable=False, unique=True, index=True)
    title: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    page_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default="note", index=True)
    path: Mapped[str] = mapped_column(Text, nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    body: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    aliases: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]"), default=list)
    tags: Mapped[list[str]] = mapped_column(ARRAY(Text), nullable=False, server_default=text("ARRAY[]::text[]"), default=list)
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class WikiLink(Base):
    __tablename__ = "wiki_links"

    id: Mapped[uuid.UUID] = _uuid_pk()
    src_page_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    dst_page_key: Mapped[str] = mapped_column(String(512), nullable=False, index=True)
    link_type: Mapped[str] = mapped_column(String(64), nullable=False, server_default="wikilink")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="unresolved", index=True)
    anchor_text: Mapped[str] = mapped_column(String(512), nullable=False, server_default="")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class WikiErrorBook(Base):
    __tablename__ = "wiki_error_book"

    id: Mapped[uuid.UUID] = _uuid_pk()
    error_type: Mapped[str] = mapped_column(String(80), nullable=False, index=True)
    page_key: Mapped[str] = mapped_column(String(512), nullable=False, server_default="", index=True)
    root_cause: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    constraint: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open", index=True)
    payload: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    fixed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class WikiCompileRun(Base):
    __tablename__ = "wiki_compile_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="running", index=True)
    pages_seen: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    pages_indexed: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    errors: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    payload: Mapped[dict[str, Any]] = _json_default()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Memory(Base, TimestampMixin):
    __tablename__ = "memories"

    id: Mapped[uuid.UUID] = _uuid_pk()
    kind: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    content: Mapped[str] = mapped_column(Text, nullable=False)
    source: Mapped[str] = mapped_column(String(32), nullable=False, server_default="explicit", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), index=True)
    archived: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), index=True)
    importance: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    confidence: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    stability: Mapped[float] = mapped_column(Float, nullable=False, server_default="0.5")
    supersedes_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True, index=True)
    source_turn_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="", index=True)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryConflict(Base):
    __tablename__ = "memory_conflicts"

    id: Mapped[uuid.UUID] = _uuid_pk()
    left_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    right_memory_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="open", index=True)
    reason: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class MemoryProbe(Base):
    __tablename__ = "memory_probes"

    id: Mapped[uuid.UUID] = _uuid_pk()
    question: Mapped[str] = mapped_column(Text, nullable=False)
    expected: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active", index=True)
    last_result: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Skill(Base, TimestampMixin):
    __tablename__ = "skills"

    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_key: Mapped[str] = mapped_column(String(256), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    path: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="active", index=True)
    pinned: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class SkillFile(Base, TimestampMixin):
    __tablename__ = "skill_files"

    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    file_path: Mapped[str] = mapped_column(Text, nullable=False)
    checksum: Mapped[str] = mapped_column(String(128), nullable=False, server_default="")
    content: Mapped[str] = mapped_column(Text, nullable=False, server_default="")


class SkillHistory(Base):
    __tablename__ = "skill_history"

    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    before_snapshot: Mapped[dict[str, Any]] = _json_default()
    after_snapshot: Mapped[dict[str, Any]] = _json_default()
    actor: Mapped[str] = mapped_column(String(80), nullable=False, server_default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)


class SkillTest(Base):
    __tablename__ = "skill_tests"

    id: Mapped[uuid.UUID] = _uuid_pk()
    skill_key: Mapped[str] = mapped_column(String(256), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(256), nullable=False)
    spec: Mapped[dict[str, Any]] = _json_default()
    last_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="never_run", index=True)
    last_result: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class EvolutionProposal(Base, TimestampMixin):
    __tablename__ = "evolution_proposals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    target_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending", index=True)
    risk_level: Mapped[str] = mapped_column(String(32), nullable=False, server_default="medium", index=True)
    payload: Mapped[dict[str, Any]] = _json_default()
    evidence: Mapped[dict[str, Any]] = _json_default()
    before_snapshot: Mapped[dict[str, Any]] = _json_default()
    after_snapshot: Mapped[dict[str, Any]] = _json_default()
    result: Mapped[dict[str, Any]] = _json_default()
    applied_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ToolRun(Base):
    __tablename__ = "tool_runs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    turn_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="", index=True)
    tool_name: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="running", index=True)
    arguments: Mapped[dict[str, Any]] = _json_default()
    result: Mapped[dict[str, Any]] = _json_default()
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class Approval(Base):
    __tablename__ = "approvals"

    id: Mapped[uuid.UUID] = _uuid_pk()
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="pending", index=True)
    subject_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    subject_id: Mapped[str] = mapped_column(String(256), nullable=False, server_default="")
    payload: Mapped[dict[str, Any]] = _json_default()
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), index=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class CronJob(Base, TimestampMixin):
    __tablename__ = "cron_jobs"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    cron_expr: Mapped[str] = mapped_column(String(80), nullable=False)
    timezone: Mapped[str] = mapped_column(String(80), nullable=False, server_default="Asia/Hong_Kong")
    instruction: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), index=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True, index=True)
    last_status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="never_run", index=True)
    last_result: Mapped[dict[str, Any]] = _json_default()
    run_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata",
        JSONB,
        nullable=False,
        server_default=text("'{}'::jsonb"),
        default=dict,
    )


class MCPServer(Base, TimestampMixin):
    __tablename__ = "mcp_servers"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    transport: Mapped[str] = mapped_column(String(32), nullable=False, server_default="stdio")
    command: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    url: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    config: Mapped[dict[str, Any]] = _json_default()
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("true"), index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="disabled", index=True)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    tool_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    tools_cache: Mapped[list[dict[str, Any]]] = mapped_column(
        JSONB,
        nullable=False,
        server_default=text("'[]'::jsonb"),
        default=list,
    )


class GatewayConnection(Base, TimestampMixin):
    __tablename__ = "gateway_connections"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    kind: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, server_default="disabled", index=True)
    endpoint: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    config: Mapped[dict[str, Any]] = _json_default()
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, server_default=text("false"), index=True)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str] = mapped_column(Text, nullable=False, server_default="")
    inbound_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    outbound_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
    failure_count: Mapped[int] = mapped_column(Integer, nullable=False, server_default="0")
