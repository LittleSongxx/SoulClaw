from __future__ import annotations

from pathlib import Path


def test_v2_app_does_not_import_legacy_runtime_packages() -> None:
    source = Path("backend/app.py").read_text(encoding="utf-8")

    forbidden_imports = [
        ".bootstrap",
        ".core",
        ".db.",
        ".agent.",
        ".memory.",
        ".skills.",
        ".wiki.",
        ".tools.",
        ".mcp.",
        ".cron.",
    ]
    for forbidden in forbidden_imports:
        assert forbidden not in source

