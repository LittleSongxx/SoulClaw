from __future__ import annotations

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


def test_readiness_reports_component_failures(tmp_path) -> None:
    settings = Settings(
        database_url=f"sqlite:///{tmp_path / 'ready.sqlite3'}",
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        workspace_dir=tmp_path / "workspace",
        workspace_seed_dir=tmp_path / "workspace_seed",
        packages_dir=tmp_path / "packages",
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
