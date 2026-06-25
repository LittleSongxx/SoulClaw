"""Platform settings for the current ZLAgent runtime."""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from pydantic import AliasChoices, Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Runtime configuration loaded from environment and `.env`.

    The canonical variables use the `ZLAGENT_` prefix. `DATABASE_URL`
    remains accepted as a backup alias, but docs and compose use
    `ZLAGENT_DATABASE_URL`.
    """

    model_config = SettingsConfigDict(
        env_file=".env",
        env_prefix="ZLAGENT_",
        case_sensitive=False,
        extra="ignore",
    )

    app_name: str = "ZLAgent"
    environment: str = "development"
    host: str = "127.0.0.1"
    port: int = 8020
    log_level: str = "INFO"
    auto_migrate: bool = True
    bootstrap_wiki_on_startup: bool = True
    bootstrap_skills_on_startup: bool = True

    data_dir: Path = Path("data")
    config_dir: Path = Path("config")
    workspace_dir: Path = Path("workspace")
    packages_dir: Path = Path(".packages")
    fastembed_cache_dir: Path = Path("data/fastembed")

    database_url: str = Field(
        default="postgresql+psycopg://zlagent:zlagent@localhost:5432/zlagent",
        validation_alias=AliasChoices("ZLAGENT_DATABASE_URL", "DATABASE_URL"),
    )
    redis_url: str = "redis://localhost:6379/0"

    qdrant_enabled: bool = True
    qdrant_url: str = "http://localhost:6333"
    qdrant_api_key: str | None = None
    qdrant_wiki_collection: str = "zlagent_wiki_chunks"
    qdrant_memory_collection: str = "zlagent_memory_items"
    qdrant_skill_collection: str = "zlagent_skill_chunks"
    qdrant_dense_model: str = "BAAI/bge-small-en-v1.5"
    qdrant_sparse_model: str = "Qdrant/bm25"

    wiki_root: Path | None = None
    skills_root: Path | None = None

    admin_username: str = "admin"
    admin_password: str = "zlagent-admin"
    jwt_secret: str = "change-me-for-production"
    jwt_algorithm: str = "HS256"
    jwt_access_token_minutes: int = 60 * 12

    openai_api_key: str | None = Field(default=None, validation_alias="OPENAI_API_KEY")
    openai_base_url: str = Field(
        default="https://api.openai.com/v1",
        validation_alias="OPENAI_BASE_URL",
    )
    openai_model: str | None = Field(default=None, validation_alias="OPENAI_MODEL")

    mcp_refresh_on_startup: bool = True
    mcp_discovery_timeout_seconds: float = 8.0
    mcp_call_timeout_seconds: float = 60.0

    dream_review_enabled: bool = True
    dream_review_cron: str = "30 3 * * *"
    dream_review_timezone: str = "Asia/Shanghai"
    dream_review_window_hours: int = 24
    dream_review_limit: int = 50

    @field_validator("data_dir", "config_dir", "workspace_dir", "packages_dir", "fastembed_cache_dir", mode="before")
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
            self.packages_dir,
            self.fastembed_cache_dir,
            self.resolved_wiki_root,
            self.resolved_skills_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    settings = Settings()
    return settings


def reset_settings_cache() -> None:
    get_settings.cache_clear()
