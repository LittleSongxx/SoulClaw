from __future__ import annotations

from pathlib import Path

from backend.domain.workspace import WorkspaceService
from backend.infra.config import Settings


def test_workspace_seed_merge_copies_missing_files_without_overwrite(tmp_path: Path) -> None:
    seed = tmp_path / "workspace_seed"
    workspace = tmp_path / "workspace"
    (seed / "memory").mkdir(parents=True)
    (seed / "knowledge" / "wiki").mkdir(parents=True)
    (seed / "SOUL.md").write_text("seed soul", encoding="utf-8")
    (seed / "memory" / "MEMORY.md").write_text("seed memory", encoding="utf-8")
    (seed / "knowledge" / "wiki" / "index.md").write_text("# Seed Index", encoding="utf-8")

    workspace.mkdir()
    (workspace / "SOUL.md").write_text("user soul", encoding="utf-8")

    settings = Settings(
        workspace_dir=workspace,
        workspace_seed_dir=seed,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        packages_dir=tmp_path / ".packages",
    )
    service = WorkspaceService(settings=settings)

    copied = service.seed_from_template()

    assert copied == 2
    assert (workspace / "SOUL.md").read_text(encoding="utf-8") == "user soul"
    assert (workspace / "memory" / "MEMORY.md").read_text(encoding="utf-8") == "seed memory"
    assert (workspace / "knowledge" / "wiki" / "index.md").read_text(encoding="utf-8") == "# Seed Index"


def test_workspace_ensure_files_uses_projection_seed(tmp_path: Path) -> None:
    seed = tmp_path / "workspace_seed"
    (seed / "memory").mkdir(parents=True)
    (seed / "USER.md").write_text("seed user", encoding="utf-8")
    (seed / "memory" / "MEMORY.md").write_text("seed memory", encoding="utf-8")

    settings = Settings(
        workspace_dir=tmp_path / "workspace",
        workspace_seed_dir=seed,
        data_dir=tmp_path / "data",
        config_dir=tmp_path / "config",
        packages_dir=tmp_path / ".packages",
    )
    service = WorkspaceService(settings=settings)

    service.ensure_files()

    assert service.read("user").content == "seed user"
    assert service.read("memory").content == "seed memory"
