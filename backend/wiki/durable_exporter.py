from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional


def export_durable_wiki(
    *,
    wiki_store: Any,
    knowledge_dir: Path,
    skill_id: Optional[str] = None,
    limit: int = 500,
) -> dict[str, Any]:
    """Export volatile answer-cache rows into an LLM Wiki markdown tree."""
    root = Path(knowledge_dir).resolve() / "wiki" / "cache"
    facts_dir = root / "facts"
    sources_dir = root / "sources"
    facts_dir.mkdir(parents=True, exist_ok=True)
    sources_dir.mkdir(parents=True, exist_ok=True)

    rows = wiki_store.list_entries(skill_id=skill_id, limit=limit)
    written = 0
    links: list[str] = []
    for row in rows:
        slug = _slug(f"{row.get('skill_id')}-{row.get('id')}-{row.get('normalized_query')}")
        fact_path = facts_dir / f"{slug}.md"
        source_path = sources_dir / f"wiki-entry-{row.get('id')}.md"
        fact_md = _render_fact_page(row, source_path)
        source_md = _render_source_page(row, fact_path)
        if _write_if_changed(fact_path, fact_md):
            written += 1
        if _write_if_changed(source_path, source_md):
            written += 1
        links.append(f"- [[facts/{slug}|{row.get('skill_id')} #{row.get('id')}]]")

    index_md = _render_index(rows, links)
    if _write_if_changed(root / "index.md", index_md):
        written += 1
    return {
        "ok": True,
        "root": str(root),
        "rows_exported": len(rows),
        "pages_written": written,
        "skill_id": skill_id,
    }


def _render_fact_page(row: dict[str, Any], source_path: Path) -> str:
    frontmatter = {
        "type": "wiki_fact",
        "wiki_entry_id": row.get("id"),
        "skill_id": row.get("skill_id"),
        "confidence": row.get("confidence", 0.5),
        "crystal_kind": row.get("crystal_kind", "answer"),
        "superseded_by": row.get("superseded_by"),
        "sources": row.get("sources") or [],
        "aliases": row.get("aliases") or [],
        "geo_path": row.get("geo_path") or "",
        "exported_at": _now(),
    }
    answer = str(row.get("answer") or "").strip()
    return (
        _frontmatter(frontmatter)
        + f"# {row.get('raw_query') or row.get('normalized_query')}\n\n"
        + f"Source: [[../sources/{source_path.stem}|wiki entry {row.get('id')}]]\n\n"
        + "## Answer\n\n"
        + (answer or "(empty)")
        + "\n\n## Audit\n\n"
        + f"- normalized_query: `{row.get('normalized_query')}`\n"
        + f"- hit_count: {row.get('hit_count')}\n"
        + f"- expires_at: {row.get('expires_at') or 'never'}\n"
    )


def _render_source_page(row: dict[str, Any], fact_path: Path) -> str:
    raw = {
        k: v
        for k, v in row.items()
        if k not in {"answer"}
    }
    return (
        _frontmatter({
            "type": "wiki_source",
            "wiki_entry_id": row.get("id"),
            "skill_id": row.get("skill_id"),
            "exported_at": _now(),
        })
        + f"# Wiki Entry {row.get('id')}\n\n"
        + f"Fact page: [[../facts/{fact_path.stem}|fact]]\n\n"
        + "```json\n"
        + json.dumps(raw, ensure_ascii=False, indent=2, sort_keys=True)
        + "\n```\n"
    )


def _render_index(rows: list[dict[str, Any]], links: list[str]) -> str:
    by_kind: dict[str, int] = {}
    for row in rows:
        kind = str(row.get("crystal_kind") or "answer")
        by_kind[kind] = by_kind.get(kind, 0) + 1
    return (
        _frontmatter({
            "type": "wiki_cache_index",
            "exported_at": _now(),
            "rows": len(rows),
            "by_kind": by_kind,
        })
        + "# Durable Wiki Cache Export\n\n"
        + "This tree is generated from `wiki_entries`; edit source rows or proposals, then export again.\n\n"
        + "## Entries\n\n"
        + ("\n".join(links) if links else "(no entries)")
        + "\n"
    )


def _frontmatter(data: dict[str, Any]) -> str:
    return "---\n" + json.dumps(data, ensure_ascii=False, indent=2, sort_keys=True) + "\n---\n\n"


def _write_if_changed(path: Path, content: str) -> bool:
    try:
        existing = path.read_text(encoding="utf-8")
    except OSError:
        existing = None
    if existing == content:
        return False
    path.write_text(content, encoding="utf-8")
    return True


def _slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:120] or "wiki-entry"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
