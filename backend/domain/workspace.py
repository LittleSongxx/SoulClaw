"""Generated Markdown projections for structured long-lived state."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus

WORKSPACE_FILE_MAP = {
    "soul": Path("SOUL.md"),
    "user": Path("USER.md"),
    "memory": Path("memory") / "MEMORY.md",
    "heartbeat": Path("HEARTBEAT.md"),
}

DEFAULT_WORKSPACE_FILES = {
    "soul": (
        "# SOUL\n\n"
        "> Generated projection. Postgres core context is authoritative; propose edits through the console/API.\n\n"
        "No approved SOUL core context has been projected yet.\n"
    ),
    "user": (
        "# USER\n\n"
        "> Generated projection. Postgres core context is authoritative; propose edits through the console/API.\n\n"
        "No approved USER core context has been projected yet.\n"
    ),
    "memory": (
        "# MEMORY\n\n"
        "> Generated projection. Postgres structured memories are authoritative; edit drafts through proposals.\n\n"
        "No approved long-term memories have been projected yet.\n"
    ),
    "heartbeat": (
        "# HEARTBEAT\n\n"
        "> Generated projection. Structured heartbeat state is authoritative; propose edits through the console/API.\n\n"
        "## Active Tasks\n\n"
        "- No active proactive tasks.\n"
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

    def ensure_files(self) -> None:
        self.root.mkdir(parents=True, exist_ok=True)
        self.seed_from_template()
        (self.root / "memory").mkdir(parents=True, exist_ok=True)
        for kind, relative_path in WORKSPACE_FILE_MAP.items():
            path = self.root / relative_path
            if not path.exists():
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(DEFAULT_WORKSPACE_FILES[kind], encoding="utf-8")

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
