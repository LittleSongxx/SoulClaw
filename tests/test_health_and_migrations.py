from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from sqlalchemy import create_engine, inspect

from backend.infra.config import Settings
from backend.infra.db import migration_status, run_alembic_upgrade
from backend.infra.health import readiness_summary


class HealthyRedis:
    def ping(self) -> bool:
        return True


class BrokenRedis:
    def ping(self) -> None:
        raise RuntimeError("redis down")


def test_sqlite_bootstrap_stamps_alembic_head(tmp_path) -> None:
    db_path = tmp_path / "soulclaw.sqlite3"
    settings = Settings(database_url=f"sqlite:///{db_path}")

    run_alembic_upgrade(settings)

    status = migration_status(settings)
    inspector = inspect(create_engine(settings.database_url))
    assert "alembic_version" in inspector.get_table_names()
    assert status["current_revision"] == status["head_revision"]
    assert status["is_current"] is True


def test_sqlite_reconciles_mixed_0011_schema_without_replaying_migrations(tmp_path) -> None:
    db_path = tmp_path / "mixed.sqlite3"
    settings = Settings(database_url=f"sqlite:///{db_path}")
    engine = create_engine(settings.database_url)
    with engine.begin() as connection:
        connection.exec_driver_sql("CREATE TABLE alembic_version (version_num VARCHAR(32) NOT NULL)")
        connection.exec_driver_sql("INSERT INTO alembic_version (version_num) VALUES ('0011_wiki_fts')")
        connection.exec_driver_sql(
            """
            CREATE TABLE wiki_pages (
                id CHAR(32) NOT NULL,
                page_key VARCHAR(512) NOT NULL,
                title VARCHAR(512) NOT NULL,
                page_type VARCHAR(64) NOT NULL DEFAULT 'note',
                path TEXT NOT NULL,
                summary TEXT NOT NULL DEFAULT '',
                body TEXT NOT NULL DEFAULT '',
                aliases JSON NOT NULL DEFAULT '[]',
                tags JSON NOT NULL DEFAULT '[]',
                confidence FLOAT NOT NULL DEFAULT 0.5,
                checksum VARCHAR(128) NOT NULL DEFAULT '',
                metadata JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE evolution_proposals (
                id CHAR(32) NOT NULL,
                target_type VARCHAR(64) NOT NULL,
                action VARCHAR(64) NOT NULL,
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                risk_level VARCHAR(32) NOT NULL DEFAULT 'medium',
                payload JSON NOT NULL DEFAULT '{}',
                evidence JSON NOT NULL DEFAULT '{}',
                before_snapshot JSON NOT NULL DEFAULT '{}',
                after_snapshot JSON NOT NULL DEFAULT '{}',
                result JSON NOT NULL DEFAULT '{}',
                applied_at DATETIME,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE background_jobs (
                id CHAR(32) NOT NULL,
                task_name VARCHAR(128) NOT NULL,
                queue_id VARCHAR(256) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'queued',
                payload JSON NOT NULL DEFAULT '{}',
                result JSON NOT NULL DEFAULT '{}',
                error TEXT NOT NULL DEFAULT '',
                triggered_by VARCHAR(128) NOT NULL DEFAULT '',
                cron_job_id CHAR(32),
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                started_at DATETIME,
                finished_at DATETIME,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE cron_jobs (
                id CHAR(32) NOT NULL,
                name VARCHAR(128) NOT NULL,
                cron_expr VARCHAR(80) NOT NULL,
                timezone VARCHAR(80) NOT NULL DEFAULT 'Asia/Hong_Kong',
                instruction TEXT NOT NULL DEFAULT '',
                enabled BOOLEAN NOT NULL DEFAULT 1,
                metadata JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE runtime_events (
                id CHAR(32) NOT NULL,
                event_type VARCHAR(128) NOT NULL,
                severity VARCHAR(16) NOT NULL DEFAULT 'info',
                session_id VARCHAR(256) NOT NULL DEFAULT '',
                turn_id VARCHAR(256) NOT NULL DEFAULT '',
                payload JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE audit_events (
                id CHAR(32) NOT NULL,
                actor_id CHAR(32),
                action VARCHAR(128) NOT NULL,
                target_type VARCHAR(64) NOT NULL,
                target_id VARCHAR(256) NOT NULL DEFAULT '',
                payload JSON NOT NULL DEFAULT '{}',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE memory_history (
                id CHAR(32) NOT NULL,
                memory_id CHAR(32),
                action VARCHAR(64) NOT NULL,
                before_snapshot JSON NOT NULL DEFAULT '{}',
                after_snapshot JSON NOT NULL DEFAULT '{}',
                actor VARCHAR(80) NOT NULL DEFAULT 'system',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE outbox_messages (
                id CHAR(32) NOT NULL,
                topic VARCHAR(128) NOT NULL,
                aggregate_type VARCHAR(64) NOT NULL,
                aggregate_id VARCHAR(128) NOT NULL,
                idempotency_key VARCHAR(256) NOT NULL DEFAULT '',
                status VARCHAR(32) NOT NULL DEFAULT 'pending',
                payload JSON NOT NULL DEFAULT '{}',
                attempt_count INTEGER NOT NULL DEFAULT 0,
                max_attempts INTEGER NOT NULL DEFAULT 5,
                next_attempt_at DATETIME,
                last_error TEXT NOT NULL DEFAULT '',
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                dispatched_at DATETIME,
                PRIMARY KEY (id)
            )
            """
        )
        connection.exec_driver_sql(
            """
            CREATE TABLE idempotency_records (
                id CHAR(32) NOT NULL,
                scope VARCHAR(128) NOT NULL,
                idempotency_key VARCHAR(256) NOT NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (id)
            )
            """
        )

    run_alembic_upgrade(settings)

    status = migration_status(settings)
    inspector = inspect(create_engine(settings.database_url))
    assert status["current_revision"] == "0016_core_context_blocks"
    assert status["is_current"] is True
    assert {"agent_runs", "agent_run_steps", "knowledge_embeddings", "policy_rules", "core_context_blocks"} <= set(inspector.get_table_names())
    assert {"block_key", "title", "content", "status", "version", "confidence", "source", "metadata"} <= _columns(
        inspector, "core_context_blocks"
    )
    assert {"claims", "source_refs", "stale_after"} <= _columns(inspector, "wiki_pages")
    assert {"target_checksum", "stale_reason"} <= _columns(inspector, "evolution_proposals")
    assert {"trace_id", "request_id", "idempotency_key", "dead_letter_reason"} <= _columns(inspector, "background_jobs")
    assert {"backoff_until", "last_enqueue_key"} <= _columns(inspector, "cron_jobs")
    assert {"trace_id", "request_id"} <= _columns(inspector, "runtime_events")
    assert {"trace_id", "request_id"} <= _columns(inspector, "audit_events")
    assert {"trace_id", "request_id", "dispatched_at"} <= _columns(inspector, "outbox_messages")
    assert {"request_hash", "status", "response", "updated_at", "expires_at"} <= _columns(inspector, "idempotency_records")
    assert "ix_outbox_messages_trace_id" in _indexes(inspector, "outbox_messages")
    assert "ix_background_jobs_idempotency_key" in _indexes(inspector, "background_jobs")
    assert "ix_cron_jobs_last_enqueue_key" in _indexes(inspector, "cron_jobs")


def test_sqlite_alembic_upgrade_head_reconciles_and_stamps(tmp_path) -> None:
    db_path = tmp_path / "alembic.sqlite3"
    env = os.environ.copy()
    env["SOULCLAW_DATABASE_URL"] = f"sqlite:///{db_path}"
    env["PYTHONPATH"] = str(Path.cwd()) + os.pathsep + env.get("PYTHONPATH", "")
    result = subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", "head"],
        cwd=Path.cwd(),
        env=env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    status = migration_status(Settings(database_url=f"sqlite:///{db_path}"))
    inspector = inspect(create_engine(f"sqlite:///{db_path}"))
    assert "memory_history" in inspector.get_table_names()
    assert {"agent_runs", "agent_run_steps", "knowledge_embeddings", "policy_rules", "core_context_blocks"} <= set(inspector.get_table_names())
    assert status["current_revision"] == "0016_core_context_blocks"
    assert status["is_current"] is True


def test_readiness_reports_component_failures(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'ready.sqlite3'}",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspace_dir=tmp_path / "workspace",
        workspace_seed_dir=tmp_path / "workspace_seed",
        packages_dir=tmp_path / "packages",
        vector_mode="disabled",
    )
    run_alembic_upgrade(settings)

    optional = readiness_summary(settings, None)
    healthy = readiness_summary(settings, HealthyRedis())
    broken = readiness_summary(settings, BrokenRedis())
    required = readiness_summary(settings.model_copy(update={"redis_required": True}), None)

    assert optional["ok"] is True
    assert optional["checks"]["redis"]["ok"] is True
    assert optional["checks"]["redis"]["required"] is False
    assert healthy["ok"] is True
    assert healthy["checks"]["database"]["ok"] is True
    assert healthy["checks"]["migrations"]["ok"] is True
    assert broken["ok"] is True
    assert broken["checks"]["redis"]["ok"] is True
    assert broken["checks"]["redis"]["status"] == "optional_unavailable"
    assert required["ok"] is False
    assert required["checks"]["redis"]["required"] is True


def _columns(inspector, table_name: str) -> set[str]:
    return {column["name"] for column in inspector.get_columns(table_name)}


def _indexes(inspector, table_name: str) -> set[str]:
    return {index["name"] for index in inspector.get_indexes(table_name)}
