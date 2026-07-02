"""LangGraph checkpoint integration for agent runs."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus


class AgentCheckpointStore:
    def __init__(self, *, settings: Settings | None = None, events: RuntimeEventBus | None = None) -> None:
        self.settings = settings or get_settings()
        self.events = events
        self._setup_done = False

    @property
    def enabled(self) -> bool:
        return self.settings.agent_engine == "langgraph" and self.settings.database_url.startswith("postgresql")

    def save(self, *, thread_id: str, run_id: str, checkpoint_id: str, state: dict[str, Any]) -> bool:
        if not self.enabled:
            return False
        try:
            from langgraph.checkpoint.base import empty_checkpoint
            from langgraph.checkpoint.postgres import PostgresSaver

            config = {"configurable": {"thread_id": thread_id, "checkpoint_ns": "", "checkpoint_id": checkpoint_id}}
            checkpoint = empty_checkpoint()
            checkpoint["channel_values"] = {"state": state}
            checkpoint["channel_versions"] = {"state": "1"}
            checkpoint["versions_seen"] = {}
            metadata = {
                "source": "soulclaw",
                "step": 0,
                "writes": {"state": state},
                "parents": {},
                "run_id": run_id,
                "saved_at": datetime.now(UTC).isoformat(),
            }
            with PostgresSaver.from_conn_string(_psycopg_conninfo(self.settings.database_url)) as saver:
                if not self._setup_done:
                    saver.setup()
                    self._setup_done = True
                saver.put(config, checkpoint, metadata, {"state": "1"})
            return True
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit("agent.checkpoint.save_failed", {"run_id": run_id, "error": str(exc)}, severity="warning")
            return False


def _psycopg_conninfo(database_url: str) -> str:
    if database_url.startswith("postgresql+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgresql+psycopg://")
    if database_url.startswith("postgres+psycopg://"):
        return "postgresql://" + database_url.removeprefix("postgres+psycopg://")
    return database_url
