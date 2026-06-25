from __future__ import annotations

from backend.infra.config import Settings
from backend.infra.security import hash_password, verify_password


def test_database_url_accepts_new_env_alias(monkeypatch) -> None:
    monkeypatch.setenv("ZLAGENT_DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/newdb")
    monkeypatch.delenv("DATABASE_URL", raising=False)

    settings = Settings()

    assert settings.database_url.endswith("/newdb")


def test_database_url_keeps_backup_alias(monkeypatch) -> None:
    monkeypatch.delenv("ZLAGENT_DATABASE_URL", raising=False)
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:p@localhost:5432/backupdb")

    settings = Settings()

    assert settings.database_url.endswith("/backupdb")


def test_core_bootstrap_defaults_are_enabled() -> None:
    settings = Settings()

    assert settings.bootstrap_wiki_on_startup is True
    assert settings.bootstrap_skills_on_startup is True
    assert settings.dream_review_enabled is True
    assert settings.dream_review_cron == "30 3 * * *"
    assert settings.dream_review_timezone == "Asia/Shanghai"


def test_password_hash_roundtrip() -> None:
    encoded = hash_password("correct horse battery staple")

    assert verify_password("correct horse battery staple", encoded)
    assert not verify_password("wrong", encoded)
