"""Platform settings for the current SoulClaw runtime."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Annotated

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment and `.env`.

    The canonical variables use the `SOULCLAW_` prefix. `DATABASE_URL`
    remains accepted as a backup alias, but docs and compose use
    `SOULCLAW_DATABASE_URL`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="SOULCLAW_",
        case_sensitive=False,
        extra="ignore",
        populate_by_name=True,
    )

    app_name: str = "SoulClaw"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    log_level: str = "INFO"
    auto_migrate: bool = True
    bootstrap_wiki_on_startup: bool = True
    bootstrap_skills_on_startup: bool = True
    cors_origins: Annotated[list[str], NoDecode] = ["*"]
    require_production_secrets: bool = True
    login_rate_limit_enabled: bool = True
    metrics_enabled: bool = True
    metrics_path: str = "/metrics"
    tracing_enabled: bool = True
    otel_exporter_otlp_endpoint: str = ""
    rate_limit_enabled: bool = True
    rate_limit_memory_fallback: bool = True
    rate_limit_admin_per_minute: int = 120
    rate_limit_turn_per_minute: int = 20
    rate_limit_gateway_per_minute: int = 120
    rate_limit_llm_per_minute: int = 60

    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    workspace_dir: Path = Path("workspace")
    workspace_seed_dir: Path = Path("workspace_seed")
    packages_dir: Path = Path(".packages")

    database_url: str = Field(
        default="postgresql+psycopg://soulclaw:soulclaw@localhost:5432/soulclaw",
        validation_alias=AliasChoices("SOULCLAW_DATABASE_URL", "DATABASE_URL"),
    )
    redis_url: str = "redis://localhost:6379/0"
    redis_required: bool = False
    celery_broker_url: str | None = None
    celery_result_backend: str | None = None
    api_scheduler_enabled: bool = False
    queue_eager: bool = False

    wiki_root: Path | None = None
    skills_root: Path | None = None

    admin_username: str = "admin"
    admin_password: str = "soulclaw-admin"
    jwt_secret: str = "change-me-for-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 60 * 12

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias="OPENAI_BASE_URL",
    )
    openai_model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")
    llm_provider: str = "openai-compatible"
    llm_context_window_tokens: int = 128000
    agent_engine: str = "langgraph"
    vector_mode: str = "required"
    embedding_model: str = "text-embedding-3-small"
    embedding_dimensions: int = 1536
    embedding_batch_size: int = 32
    embedding_pass_dimensions: bool = True

    mcp_refresh_on_startup: bool = True
    mcp_seed_on_startup: bool = True
    mcp_config_file: Path = Path("config/mcp_servers.yaml")
    mcp_discovery_timeout_seconds: float = 8.0
    mcp_call_timeout_seconds: float = 60.0
    tool_schema_direct_limit: int = 32

    dream_review_enabled: bool = True
    dream_review_cron: str = "30 3 * * *"
    dream_review_timezone: str = "Asia/Shanghai"
    dream_review_window_hours: int = 24
    dream_review_limit: int = 50
    heartbeat_enabled: bool = True
    heartbeat_cron: str = "*/30 * * * *"
    heartbeat_timezone: str = "Asia/Shanghai"
    gateway_heartbeat_timeout_seconds: int = 120
    gateway_webhook_max_skew_seconds: int = 300
    gateway_webhook_nonce_cache_size: int = 200
    public_base_url: str = ""
    a2a_http_timeout_seconds: float = 60.0
    a2a_bootstrap_soulsearcher_enabled: bool = False
    a2a_soulsearcher_base_url: str = "http://127.0.0.1:8001"
    a2a_soulsearcher_internal_api_key: str = ""
    a2a_soulsearcher_auth_user_header: str = "X-SoulSearcher-User"
    a2a_soulsearcher_user_id: str = "soulclaw"
    a2a_public_api_key: str = ""
    a2a_require_public_auth: bool = True
    a2a_poll_interval_seconds: float = 5.0
    a2a_stalled_timeout_seconds: int = 900
    a2a_callback_public_url: str = ""
    a2a_callback_secret: str = ""
    a2a_live_event_idle_seconds: int = 30

    @property
    def resolved_celery_broker_url(self) -> str:
        return self.celery_broker_url or self.redis_url

    @property
    def resolved_celery_result_backend(self) -> str:
        return self.celery_result_backend or self.redis_url

    @field_validator("cors_origins", mode="before")
    @classmethod
    def _split_cors_origins(cls, value: str | list[str]) -> list[str]:
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith("["):
                loaded = json.loads(stripped)
                if isinstance(loaded, list):
                    return [str(item).strip() for item in loaded if str(item).strip()]
            return [item.strip() for item in value.split(",") if item.strip()]
        return value

    @field_validator(
        "data_dir",
        "config_dir",
        "workspace_dir",
        "workspace_seed_dir",
        "packages_dir",
        "mcp_config_file",
        mode="before",
    )
    @classmethod
    def _expand_path(cls, value: str | Path) -> Path:
        return Path(value).expanduser()

    @property
    def resolved_wiki_root(self) -> Path:
        root = self.wiki_root or (self.workspace_dir / "knowledge" / "wiki")
        return root.expanduser()

    @property
    def resolved_skills_root(self) -> Path:
        root = self.skills_root or (self.workspace_dir / "skills")
        return root.expanduser()

    def ensure_directories(self) -> None:
        for path in (
            self.data_dir,
            self.config_dir,
            self.workspace_dir,
            self.workspace_seed_dir,
            self.packages_dir,
            self.resolved_wiki_root,
            self.resolved_skills_root,
            self.workspace_dir / "memory",
        ):
            path.mkdir(parents=True, exist_ok=True)

    def validate_runtime_secrets(self) -> None:
        if self.environment.lower() not in {"production", "prod"} or not self.require_production_secrets:
            return
        if self.database_url.startswith("sqlite"):
            raise RuntimeError("SQLite is only allowed for tests or explicit lightweight fallback; set SOULCLAW_DATABASE_URL to Postgres in production")
        if self.jwt_secret == "change-me-for-production":
            raise RuntimeError("SOULCLAW_JWT_SECRET must be changed in production")
        if self.admin_password == "soulclaw-admin":
            raise RuntimeError("SOULCLAW_ADMIN_PASSWORD must be changed in production")
        if not self.cors_origins or "*" in self.cors_origins:
            raise RuntimeError("SOULCLAW_CORS_ORIGINS must be explicit in production")
        if not self.public_base_url:
            raise RuntimeError("SOULCLAW_PUBLIC_BASE_URL must be set in production")

    @property
    def vector_required(self) -> bool:
        return self.vector_mode.lower() == "required"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
