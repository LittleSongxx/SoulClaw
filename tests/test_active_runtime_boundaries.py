from __future__ import annotations

import re
from pathlib import Path


def test_app_uses_active_runtime_packages() -> None:
    source = Path("backend/app.py").read_text(encoding="utf-8")

    forbidden_imports = [
        r"from \.bootstrap",
        r"from \.core",
        r"from \.db\.",
        r"from \.agent\.",
        r"from \.memory\.",
        r"from \.skills\.",
        r"from \.wiki\.",
        r"from \.tools\.",
        r"from \.mcp\.",
        r"from \.cron\.",
    ]
    for forbidden in forbidden_imports:
        assert re.search(forbidden, source) is None


def test_project_copy_does_not_use_generation_labels() -> None:
    checked_roots = [
        Path("README.md"),
        Path("README.en.md"),
        Path(".env.example"),
        Path("Dockerfile"),
        Path("docker-entrypoint.py"),
        Path("config"),
        Path("backend"),
        Path("workspace_seed"),
        Path("tests"),
    ]
    allowed_patterns = (
        re.compile(r"version\s*[:=]"),
        re.compile(r"__version__"),
        re.compile(r"Node\.js"),
        re.compile(r"BAAI/bge-small-en-v1\.5"),
        re.compile(r"https?://\S*"),
        re.compile(r"OPENAI_BASE_URL=.*"),
        re.compile(r"forbidden = re\.compile"),
        re.compile(r"allowed_patterns ="),
        re.compile(r"re\.compile"),
    )
    forbidden = re.compile(r"\b(?:v1|v2|v0\.\d+|legacy|Legacy|pre-v2|旧版)\b")
    offenders: list[str] = []
    for root in checked_roots:
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            if path.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".lock"}:
                continue
            for line_no, line in enumerate(path.read_text(encoding="utf-8").splitlines(), start=1):
                if not forbidden.search(line):
                    continue
                if any(pattern.search(line) for pattern in allowed_patterns):
                    continue
                offenders.append(f"{path}:{line_no}:{line.strip()}")
    assert offenders == []


def test_no_weaver_a2a_bootstrap_residue() -> None:
    checked_roots = [
        Path("README.md"),
        Path("README.en.md"),
        Path(".env.example"),
        Path(".env.production.example"),
        Path("docker-compose.yml"),
        Path("docker-compose.prod.yml"),
        Path("backend"),
        Path("config"),
        Path("tests"),
    ]
    forbidden = "A2A_BOOTSTRAP_" + "WEAVER"
    offenders: list[str] = []
    for root in checked_roots:
        paths = [root] if root.is_file() else [path for path in root.rglob("*") if path.is_file()]
        for path in paths:
            if path.suffix in {".pyc", ".png", ".jpg", ".jpeg", ".lock"}:
                continue
            if forbidden in path.read_text(encoding="utf-8"):
                offenders.append(str(path))
    assert offenders == []
