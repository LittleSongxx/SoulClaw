"""Markdown-authoritative workspace files for long-lived agent state."""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import EvolutionProposal

WORKSPACE_FILE_MAP = {
    "soul": Path("SOUL.md"),
    "user": Path("USER.md"),
    "memory": Path("memory") / "MEMORY.md",
    "heartbeat": Path("HEARTBEAT.md"),
}

DEFAULT_WORKSPACE_FILES = {
    "soul": (
        "# SOUL\n\n"
        "SoulClaw is a local-first long-term personal assistant. It should be careful, useful, "
        "transparent about uncertainty, and respectful of the user's preferences.\n"
    ),
    "user": (
        "# USER\n\n"
        "This file stores durable user preferences, profile facts, and relationship context. "
        "Only write stable information here after review.\n"
    ),
    "memory": (
        "# MEMORY\n\n"
        "Durable lessons, user facts, project knowledge, and recurring patterns live here. "
        "Use readable Markdown; the database is only a searchable mirror.\n"
    ),
    "heartbeat": (
        "# HEARTBEAT\n\n"
        "## Active Tasks\n\n"
        "- Keep this section empty when no proactive background check is needed.\n"
    ),
}


@dataclass(frozen=True)
class WorkspaceFile:
    kind: str
    path: str
    content: str
    updated_at: str | None


class WorkspaceService:
    def __init__(self, settings: Settings | None = None, events: RuntimeEventBus | None = None) -> None:
        self.settings = settings or get_settings()
        self.events = events

    @property
    def root(self) -> Path:
        return self.settings.workspace_dir.expanduser()

    @property
    def seed_root(self) -> Path:
        return self.settings.workspace_seed_dir.expanduser()

    @property
    def history_path(self) -> Path:
        return self.root / "memory" / "history.jsonl"

    def ensure_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed_from_template()
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        for kind, relative_path in WORKSPACE_FILE_MAP.items():
            path = self.root / relative_path
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_WORKSPACE_FILES[kind], encoding="utf-8")
        if not self.history_path.exists():
            self.history_path.parent.mkdir(parents=True, exist_ok=True)
            self.history_path.write_text("", encoding="utf-8")

    def seed_from_template(self) -> int:
        return self._copy_missing_tree(self.seed_root, self.root)

    def read(self, kind: str) -> WorkspaceFile:
        path = self._path_for(kind)
        if not path.exists():
            self.ensure_files()
        content = path.read_text(encoding="utf-8") if path.exists() else ""
        updated_at = datetime.fromtimestamp(path.stat().st_mtime, UTC).isoformat() if path.exists() else None
        return WorkspaceFile(
            kind=kind,
            path=path.relative_to(self.root).as_posix(),
            content=content,
            updated_at=updated_at,
        )

    def read_all(self) -> dict[str, WorkspaceFile]:
        self.ensure_files()
        return {kind: self.read(kind) for kind in WORKSPACE_FILE_MAP}

    def write(self, kind: str, content: str, *, actor: str = "admin") -> WorkspaceFile:
        path = self._path_for(kind)
        path.parent.mkdir(parents=True, exist_ok=True)
        before = path.read_text(encoding="utf-8") if path.exists() else ""
        path.write_text(content, encoding="utf-8")
        item = self.read(kind)
        if self.events:
            self.events.emit("workspace.file.updated", {"kind": kind, "path": item.path, "actor": actor})
            self.events.audit(
                "workspace.file.update",
                "workspace_file",
                target_id=kind,
                payload={"path": item.path, "actor": actor, "before_len": len(before), "after_len": len(content)},
            )
        return item

    def append_history(self, record: dict[str, Any]) -> None:
        self.ensure_files()
        payload = {"created_at": datetime.now(UTC).isoformat(), **record}
        with self.history_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, ensure_ascii=False, default=str) + "\n")

    def read_history(self, *, limit: int = 50) -> list[dict[str, Any]]:
        self.ensure_files()
        lines = self.history_path.read_text(encoding="utf-8").splitlines()
        items: list[dict[str, Any]] = []
        for line in lines[-max(1, min(limit, 500)) :]:
            if not line.strip():
                continue
            try:
                loaded = json.loads(line)
            except json.JSONDecodeError:
                loaded = {"raw": line}
            if isinstance(loaded, dict):
                items.append(loaded)
        return items

    def apply_proposal(self, db: Session, proposal_id, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not applyable: {proposal.status}")
        if proposal.target_type not in {"persona", "user", "heartbeat", "workspace_file"}:
            raise ValueError(f"unsupported workspace proposal target_type: {proposal.target_type}")
        payload = proposal.payload or {}
        kind = str(payload.get("kind") or self._kind_for_target(proposal.target_type))
        before = self.read(kind)
        if proposal.action in {"replace_file", "update_file"}:
            content = str(payload.get("content") or "")
        elif proposal.action == "append_file":
            content = before.content.rstrip() + "\n\n" + str(payload.get("content") or "").strip() + "\n"
        else:
            raise ValueError("workspace proposal action must be replace_file, update_file, or append_file")
        after = self.write(kind, content, actor=actor)
        result = {"ok": True, "kind": kind, "path": after.path, "actor": actor, "action": proposal.action}
        proposal.status = "applied"
        proposal.before_snapshot = {"kind": kind, "path": before.path, "content": before.content}
        proposal.after_snapshot = {"kind": kind, "path": after.path, "content": after.content}
        proposal.result = result
        proposal.applied_at = datetime.now(UTC)
        if self.events:
            self.events.emit("workspace.proposal.applied", {"proposal_id": str(proposal.id), **result})
            self.events.audit(
                "workspace.proposal.apply",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload=result,
            )
        return proposal

    def active_heartbeat_tasks(self) -> list[str]:
        content = self.read("heartbeat").content
        lines = content.splitlines()
        in_active = False
        tasks: list[str] = []
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("## "):
                in_active = stripped.lower() == "## active tasks"
                continue
            if in_active and stripped.startswith("-"):
                item = stripped.removeprefix("-").strip()
                if item and "empty when no proactive" not in item.lower():
                    tasks.append(item)
        return tasks

    def _path_for(self, kind: str) -> Path:
        normalized = kind.strip().lower()
        relative = WORKSPACE_FILE_MAP.get(normalized)
        if relative is None:
            raise KeyError(f"unsupported workspace file kind: {kind}")
        return self.root / relative

    def _copy_missing_tree(self, src: Path, dst: Path) -> int:
        copied = 0
        if not src.exists():
            return copied
        dst.mkdir(parents=True, exist_ok=True)
        for item in src.iterdir():
            target = dst / item.name
            if item.is_dir():
                copied += self._copy_missing_tree(item, target)
                continue
            if target.exists():
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)
            copied += 1
        return copied

    @staticmethod
    def _kind_for_target(target_type: str) -> str:
        if target_type == "persona":
            return "soul"
        if target_type == "workspace_file":
            return "memory"
        return target_type
