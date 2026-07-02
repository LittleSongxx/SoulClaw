"""Markdown Wiki compiler and retrieval service."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, desc, func, or_, select, text
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import (
    EvolutionProposal,
    WikiCompileRun,
    WikiErrorBook,
    WikiLink,
    WikiPage,
    WikiSource,
)
from backend.domain.vector import KnowledgeVectorService

FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
CANONICAL_WIKI_FILES = {"SCHEMA.md", "index.md", "log.md"}
SPECIAL_WIKI_DIRS = {"raw", "entities", "concepts", "comparisons", "queries", "_archive"}
WIKI_PROPOSAL_ACTIONS = {"create_page", "update_page", "archive_page", "fix_link", "update_index", "append_log"}
LOW_RISK_REPAIR_TYPES = {"missing_canonical_file", "missing_index_entry", "orphan_page"}
HIGH_RISK_REPAIR_TYPES = {
    "broken_link",
    "contested_claim",
    "dangling_link",
    "duplicate_page_key",
    "insufficient_evidence",
    "low_confidence",
    "source_drift",
    "stale_page",
}
BROWSE_FIRST_TERMS = {
    "all",
    "catalog",
    "directory",
    "index",
    "list",
    "map",
    "overview",
    "schema",
    "什么",
    "全部",
    "列出",
    "有哪些",
    "概览",
    "目录",
    "索引",
}
BRIDGE_TERMS = {
    "compare",
    "compose",
    "connect",
    "difference",
    "relationship",
    "tradeoff",
    "why",
    "关系",
    "区别",
    "取舍",
    "如何",
    "为什么",
    "比较",
    "综合",
    "联系",
}
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "for",
    "how",
    "in",
    "is",
    "of",
    "or",
    "the",
    "to",
    "what",
    "with",
}
DEFAULT_CANONICAL_CONTENT = {
    "SCHEMA.md": """---
title: Wiki Schema
page_key: schema
type: schema
tags:
  - llm-wiki
confidence: 0.6
summary: Control rules for the local LLM-Wiki.
---

# Wiki Schema

Use Markdown frontmatter, stable page keys, summaries, wikilinks, and explicit sources so agents can browse, search, verify, and repair this Wiki.
""",
    "index.md": """---
title: Wiki Index
page_key: index
type: index
tags:
  - llm-wiki
confidence: 0.6
summary: Root map for the local LLM-Wiki.
---

# Wiki Index

Use this page as the browse-first map for durable Wiki knowledge.
""",
    "log.md": "# Wiki Log\n",
}


def normalize_page_key(value: str) -> str:
    value = value.strip().replace("\\", "/")
    value = re.sub(r"\.md$", "", value, flags=re.IGNORECASE)
    value = value.lower()
    value = re.sub(r"\s+", "-", value)
    value = re.sub(r"[^a-z0-9_\-/]+", "-", value)
    value = re.sub(r"-{2,}", "-", value)
    return value.strip("-/")


def _listify(value: Any) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return [str(value).strip()]


def _checksum(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _first_heading(body: str) -> str | None:
    match = HEADING_RE.search(body)
    if not match:
        return None
    return match.group(1).strip()


def _summary_from_body(body: str) -> str:
    for block in body.split("\n\n"):
        block = block.strip()
        if not block or block.startswith("#"):
            continue
        return re.sub(r"\s+", " ", block)[:500]
    return ""


def _query_tokens(value: str) -> list[str]:
    lowered = value.lower()
    tokens = re.findall(r"[\w\-/]+|[\u4e00-\u9fff]+", lowered)
    return [token.strip("-_/") for token in tokens if len(token.strip("-_/")) > 1 and token not in STOPWORDS]


def _field_contains(field: str, query: str, tokens: list[str]) -> bool:
    lowered = field.lower()
    if query and query.lower() in lowered:
        return True
    return any(token in lowered for token in tokens)


def _snippet(text_value: str, query: str, *, limit: int = 260) -> str:
    compact = re.sub(r"\s+", " ", text_value or "").strip()
    if not compact:
        return ""
    lowered = compact.lower()
    needles = [query.lower(), *_query_tokens(query)]
    start = 0
    for needle in needles:
        if not needle:
            continue
        index = lowered.find(needle)
        if index >= 0:
            start = max(0, index - 70)
            break
    excerpt = compact[start : start + limit].strip()
    prefix = "..." if start > 0 else ""
    suffix = "..." if start + limit < len(compact) else ""
    return f"{prefix}{excerpt}{suffix}"


def _safe_fts_query(value: str) -> str:
    tokens = _query_tokens(value)
    if not tokens:
        cleaned = re.sub(r'"', " ", value).strip()
        return f'"{cleaned}"' if cleaned else ""
    return " OR ".join(f'"{token.replace(chr(34), chr(32))}"' for token in tokens)


def _result_scalar(result: Any, default: Any = None) -> Any:
    for method in ("scalar_one_or_none", "scalar"):
        if hasattr(result, method):
            try:
                value = getattr(result, method)()
                return default if value is None else value
            except Exception:  # noqa: BLE001
                continue
    if hasattr(result, "first"):
        try:
            row = result.first()
        except Exception:  # noqa: BLE001
            return default
        if row is None:
            return default
        if isinstance(row, tuple):
            return row[0]
        return row
    return default


def _confidence(value: Any) -> float:
    try:
        return max(0.0, min(1.0, float(value if value is not None else 0.0)))
    except (TypeError, ValueError):
        return 0.0


def _json_list(value: Any) -> list[dict[str, Any]]:
    if not isinstance(value, list):
        return []
    return [item for item in value if isinstance(item, dict)]


def _parse_datetime(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except ValueError:
        return None


@dataclass(frozen=True)
class ParsedWikiPage:
    page_key: str
    title: str
    page_type: str
    relative_path: str
    absolute_path: Path
    summary: str
    body: str
    aliases: list[str]
    tags: list[str]
    confidence: float
    claims: list[dict[str, Any]]
    source_refs: list[dict[str, Any]]
    stale_after: datetime | None
    metadata: dict[str, Any]
    checksum: str
    links: list[tuple[str, str]]


def parse_markdown_page(path: Path, root: Path) -> ParsedWikiPage:
    raw = path.read_text(encoding="utf-8")
    metadata: dict[str, Any] = {}
    body = raw
    match = FRONTMATTER_RE.match(raw)
    if match:
        loaded = yaml.safe_load(match.group("yaml")) or {}
        if not isinstance(loaded, dict):
            raise ValueError("frontmatter must be a mapping")
        metadata = dict(loaded)
        body = match.group("body")

    relative_path = path.relative_to(root).as_posix()
    default_key = normalize_page_key(str(Path(relative_path).with_suffix("")))
    page_key = normalize_page_key(str(metadata.get("page_key") or metadata.get("id") or default_key))
    title = str(metadata.get("title") or _first_heading(body) or Path(relative_path).stem).strip()
    page_type = str(metadata.get("type") or metadata.get("page_type") or "note").strip() or "note"
    aliases = _listify(metadata.get("aliases"))
    tags = _listify(metadata.get("tags"))
    summary = str(metadata.get("summary") or _summary_from_body(body)).strip()
    try:
        confidence = float(metadata.get("confidence", 0.5))
    except (TypeError, ValueError):
        confidence = 0.5
    confidence = max(0.0, min(confidence, 1.0))
    claims = _json_list(metadata.get("claims"))
    source_refs = _json_list(metadata.get("source_refs") or metadata.get("evidence"))
    stale_after = _parse_datetime(metadata.get("stale_after") or metadata.get("expires_at"))

    links: list[tuple[str, str]] = []
    for link_match in WIKILINK_RE.finditer(body):
        target = normalize_page_key(link_match.group(1))
        anchor = (link_match.group(2) or link_match.group(1)).strip()
        if target:
            links.append((target, anchor))

    return ParsedWikiPage(
        page_key=page_key,
        title=title,
        page_type=page_type,
        relative_path=relative_path,
        absolute_path=path,
        summary=summary,
        body=body,
        aliases=aliases,
        tags=tags,
        confidence=confidence,
        claims=claims,
        source_refs=source_refs,
        stale_after=stale_after,
        metadata=metadata,
        checksum=_checksum(raw),
        links=links,
    )


class WikiService:
    def __init__(
        self,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
        vector: KnowledgeVectorService | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.events = events
        self.vector = vector

    @property
    def root(self) -> Path:
        return self.settings.resolved_wiki_root

    def scan(self) -> list[ParsedWikiPage]:
        self.root.mkdir(parents=True, exist_ok=True)
        pages: list[ParsedWikiPage] = []
        for path in self._markdown_paths():
            pages.append(parse_markdown_page(path, self.root))
        return pages

    def orientation(self, db: Session, *, recent_log_lines: int = 40) -> dict[str, Any]:
        schema = self._read_optional_file("SCHEMA.md")
        index = self._read_optional_file("index.md")
        log = self._read_optional_file("log.md")
        pages = self.list_pages(db, limit=500)
        recent_log = "\n".join(log.splitlines()[-max(1, min(recent_log_lines, 200)) :])
        open_constraints = [
            {
                "error_type": item.error_type,
                "page_key": item.page_key,
                "root_cause": item.root_cause,
                "constraint": item.constraint,
                "constraint_rule": getattr(item, "constraint_rule", "") or "",
                "verification_method": getattr(item, "verification_method", "") or "",
                "source_refs": getattr(item, "source_refs", []) or [],
            }
            for item in self.error_book(db, status="open", limit=50)
        ]
        return {
            "root": str(self.root),
            "schema": schema,
            "index": index,
            "recent_log": recent_log,
            "page_count": len(pages),
            "directories": sorted({Path(page.path).parts[0] for page in pages if Path(page.path).parts}),
            "page_types": sorted({page.page_type for page in pages}),
            "fts": self.fts_status(db),
            "route_rules": {
                "search_first": "Use for named entities, exact page keys, aliases, tags, and focused facts.",
                "browse_first": "Use for open-ended inventory, overview, map, directory, and schema questions.",
                "bridge": "Use for comparisons, relationships, multi-hop questions, and synthesis across pages.",
                "insufficient": "Use when the Wiki has no plausible page-level evidence or open constraints block trust.",
            },
            "open_constraints": open_constraints,
            "pages": [
                {
                    "page_key": page.page_key,
                    "title": page.title,
                    "page_type": page.page_type,
                    "path": page.path,
                    "summary": page.summary,
                    "tags": page.tags,
                    "confidence": _confidence(page.confidence),
                }
                for page in pages[:120]
            ],
        }

    def compile(self, db: Session) -> dict[str, Any]:
        run = WikiCompileRun(status="running", pages_seen=0, pages_indexed=0, errors=0, payload={})
        db.add(run)
        db.flush()

        paths = self._markdown_paths()
        parsed_pages: list[ParsedWikiPage] = []
        errors: list[dict[str, Any]] = []
        source_keys = {path.relative_to(self.root).as_posix() for path in paths}
        seen_page_keys: set[str] = set()
        new_errors: list[WikiErrorBook] = []
        for path in paths:
            relative_path = path.relative_to(self.root).as_posix()
            try:
                page = parse_markdown_page(path, self.root)
            except Exception as exc:  # noqa: BLE001
                errors.append({"type": "parse_error", "path": relative_path, "message": str(exc)})
                new_errors.append(
                    self._error(
                        error_type="compile_error",
                        page_key=relative_path,
                        root_cause=str(exc),
                        constraint="Fix Markdown frontmatter/body so the compiler can parse this source.",
                        payload={"path": relative_path},
                    )
                )
                continue
            if page.page_key in seen_page_keys:
                errors.append({"type": "duplicate_page_key", "path": relative_path, "page_key": page.page_key})
                new_errors.append(
                    self._error(
                        error_type="duplicate_page_key",
                        page_key=page.page_key,
                        root_cause=f"Duplicate page_key declared by {relative_path}",
                        constraint="Give each Markdown Wiki page a unique page_key.",
                        payload={"path": relative_path},
                    )
                )
                continue
            seen_page_keys.add(page.page_key)
            parsed_pages.append(page)

        known: dict[str, str] = {}
        for page in parsed_pages:
            known[page.page_key] = page.page_key
            for alias in page.aliases:
                known[normalize_page_key(alias)] = page.page_key
            known[normalize_page_key(page.title)] = page.page_key

        db.execute(delete(WikiLink))
        now = datetime.now(UTC)
        for old_error in db.scalars(
            select(WikiErrorBook).where(
                WikiErrorBook.error_type.in_(("dangling_link", "compile_error", "duplicate_page_key")),
                WikiErrorBook.status == "open",
            )
        ):
            old_error.status = "fixed"
            old_error.lifecycle_status = "fixed"
            old_error.fixed_at = now

        for error in new_errors:
            db.add(error)

        current_page_keys = {page.page_key for page in parsed_pages}
        if not errors:
            if current_page_keys:
                db.execute(delete(WikiPage).where(WikiPage.page_key.not_in(current_page_keys)))
            else:
                db.execute(delete(WikiPage))
            if source_keys:
                db.execute(delete(WikiSource).where(WikiSource.source_key.not_in(source_keys)))
            else:
                db.execute(delete(WikiSource))

        for page in parsed_pages:
            source = db.scalar(select(WikiSource).where(WikiSource.source_key == page.relative_path))
            if source is None:
                source = WikiSource(source_key=page.relative_path)
                db.add(source)
            source.kind = "markdown"
            source.uri = str(page.absolute_path)
            source.checksum = page.checksum
            source.metadata_json = {"page_key": page.page_key}

            record = db.scalar(select(WikiPage).where(WikiPage.page_key == page.page_key))
            if record is None:
                record = WikiPage(page_key=page.page_key, path=page.relative_path, title=page.title)
                db.add(record)
            record.title = page.title
            record.page_type = page.page_type
            record.path = page.relative_path
            record.summary = page.summary
            record.body = page.body
            record.aliases = page.aliases
            record.tags = page.tags
            record.confidence = page.confidence
            record.claims = page.claims
            record.source_refs = page.source_refs
            record.stale_after = page.stale_after
            record.checksum = page.checksum
            record.metadata_json = page.metadata

            for target, anchor in page.links:
                resolved = known.get(target)
                db.add(
                    WikiLink(
                        src_page_key=page.page_key,
                        dst_page_key=resolved or target,
                        link_type="wikilink",
                        status="resolved" if resolved else "unresolved",
                        anchor_text=anchor,
                        metadata_json={"raw_target": target},
                    )
                )
                if not resolved:
                    db.add(
                        self._error(
                            error_type="dangling_link",
                            page_key=page.page_key,
                            root_cause=f"Unresolved wiki link: {target}",
                            constraint="Create the target page or add an alias/page_key matching the link.",
                            payload={"target": target, "anchor": anchor},
                        )
                    )

        db.flush()
        fts_result = (
            self.refresh_fts(db, parsed_pages)
            if not errors
            else {"backend": self._db_backend(db), "available": False, "indexed_pages": 0, "fallback": True, "skipped": "compile_error"}
        )
        vector_result = {"enabled": self.vector is not None, "embedded": 0, "failed": 0, "skipped": 0}
        if self.vector is not None and not errors:
            indexed_records = list(db.scalars(select(WikiPage).where(WikiPage.page_key.in_(current_page_keys))).all())
            try:
                vector_result = self.vector.upsert_wiki_pages(db, indexed_records, strict=False)
                self.vector.purge_source(db, source_type="wiki", source_ids=current_page_keys)
            except Exception as exc:  # noqa: BLE001
                vector_result = {"enabled": True, "embedded": 0, "failed": len(indexed_records), "skipped": 0, "error": str(exc)}
                if self.events:
                    self.events.emit("wiki.vector.refresh_failed", vector_result, severity="warning")
        run.status = "ok" if not errors else "error"
        run.pages_seen = len(source_keys)
        run.pages_indexed = len(parsed_pages)
        run.errors = len(errors)
        run.finished_at = datetime.now(UTC)
        run.payload = {
            "root": str(self.root),
            "errors": errors,
            "index": "page_mirror",
            "fts": fts_result,
            "vector": vector_result,
        }
        if self.events:
            self.events.emit("wiki.compile", run.payload, severity="info" if not errors else "warning")
        return {
            "status": run.status,
            "pages_seen": run.pages_seen,
            "pages_indexed": run.pages_indexed,
            "errors": run.errors,
            "fts": fts_result,
            "vector": vector_result,
            "llm_wiki": self._wiki_shape(),
        }

    def list_pages(self, db: Session, limit: int = 200) -> list[WikiPage]:
        limit = max(1, min(limit, 1000))
        stmt = select(WikiPage).order_by(WikiPage.page_key).limit(limit)
        return list(db.scalars(stmt).all())

    def read(self, db: Session, page_key: str) -> WikiPage | None:
        key = normalize_page_key(page_key)
        return db.scalar(select(WikiPage).where(WikiPage.page_key == key))

    def read_with_graph(self, db: Session, page_key: str) -> dict[str, Any] | None:
        page = self.read(db, page_key)
        if page is None:
            return None
        return {
            "page": page,
            "outlinks": self.follow_links(db, page.page_key, direction="out"),
            "backlinks": self.follow_links(db, page.page_key, direction="in"),
        }

    def search(
        self,
        db: Session,
        query: str,
        limit: int = 10,
        *,
        strategy: str = "",
        path_prefix: str = "",
        include_constraints: bool = False,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        query = query.strip()
        tokens = _query_tokens(query)
        fts_hits = {item["page_key"]: item for item in self._fts_search(db, query, limit=max(limit * 4, 20))}
        pages = self._candidate_pages(
            db,
            query=query,
            path_prefix=path_prefix,
            page_keys=set(fts_hits),
            limit=max(limit * 8, 200),
        )
        link_counts = self._link_counts(db)
        records: list[dict[str, Any]] = []
        for page in pages:
            fts_hit = fts_hits.get(page.page_key)
            score, matched_fields, match_reasons = self._score_page(
                page,
                query=query,
                tokens=tokens,
                fts_rank=float((fts_hit or {}).get("rank") or 0.0),
                link_counts=link_counts,
            )
            if query and score <= 0 and not fts_hit:
                continue
            source = "wiki_fts" if fts_hit else "page_index"
            snippet = str((fts_hit or {}).get("snippet") or "") or self._page_snippet(page, query)
            constraints = self._constraints_for_pages(db, [page.page_key]) if include_constraints else []
            records.append(
                {
                    "score": score,
                    "source": source,
                    "page_key": page.page_key,
                    "title": page.title,
                    "path": page.path,
                    "summary": page.summary,
                    "tags": page.tags or [],
                    "page_type": page.page_type,
                    "confidence": _confidence(page.confidence),
                    "match_reasons": match_reasons,
                    "matched_fields": matched_fields,
                    "snippet": snippet,
                    "next_actions": self._next_actions_for_search_hit(page, strategy=strategy, constraints=constraints),
                    "constraints": constraints,
                    "page": page,
                }
            )

        if self.vector is not None and query:
            try:
                vector_hits = self.vector.search(db, query=query, source_type="wiki", limit=max(limit * 3, 10))
                by_key = {item["page_key"]: item for item in records}
                for hit in vector_hits:
                    page = self.read(db, str(hit.get("source_id") or ""))
                    if page is None:
                        continue
                    vector_score = float(hit.get("vector_score") or 0.0)
                    existing = by_key.get(page.page_key)
                    if existing is not None:
                        fts_score = float(existing.get("score") or 0.0)
                        existing["vector_score"] = round(vector_score, 4)
                        existing["fts_score"] = round(fts_score, 4)
                        existing["hybrid_score"] = round(vector_score * 0.7 + fts_score * 0.3, 4)
                        existing["chunk_key"] = hit.get("chunk_key") or ""
                        existing["source"] = "wiki_hybrid"
                        if "vector" not in existing["match_reasons"]:
                            existing["match_reasons"].append("vector")
                        continue
                    constraints = self._constraints_for_pages(db, [page.page_key]) if include_constraints else []
                    records.append(
                        {
                            "score": round(vector_score * 0.7, 4),
                            "hybrid_score": round(vector_score * 0.7, 4),
                            "vector_score": round(vector_score, 4),
                            "fts_score": 0.0,
                            "chunk_key": hit.get("chunk_key") or "",
                            "source": "wiki_vector",
                            "page_key": page.page_key,
                            "title": page.title,
                            "path": page.path,
                            "summary": page.summary,
                            "tags": page.tags or [],
                            "page_type": page.page_type,
                            "confidence": _confidence(page.confidence),
                            "match_reasons": ["vector"],
                            "matched_fields": ["embedding"],
                            "snippet": self._page_snippet(page, query),
                            "next_actions": self._next_actions_for_search_hit(page, strategy=strategy, constraints=constraints),
                            "constraints": constraints,
                            "page": page,
                        }
                    )
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("wiki.vector.search_failed", {"query": query, "error": str(exc)}, severity="warning")
        for record in records:
            record.setdefault("fts_score", round(float(record.get("score") or 0.0), 4))
            record.setdefault("vector_score", 0.0)
            record.setdefault("hybrid_score", round(float(record.get("score") or 0.0), 4))
            record.setdefault("chunk_key", "")
        records.sort(key=lambda item: (item["hybrid_score"], _confidence(item.get("confidence")), item["page_key"]), reverse=True)
        return records[:limit]

    def route(self, db: Session, query: str, *, limit: int = 10) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        query = query.strip()
        pages = self.list_pages(db, limit=500)
        if not query or not pages:
            return {
                "query": query,
                "strategy": "insufficient",
                "required_fan_in": 0,
                "recommended_steps": ["wiki_orient"],
                "reason": "Wiki is empty or query is blank.",
                "items": [],
                "open_constraints": self._constraints_for_query(db, query, []),
            }

        lower = query.lower()
        hits = self.search(db, query, limit=limit, include_constraints=True)
        browse_intent = any(term in lower for term in BROWSE_FIRST_TERMS)
        bridge_intent = any(term in lower for term in BRIDGE_TERMS)
        if bridge_intent:
            strategy = "bridge"
            fan_in = min(3, max(2, len(hits) or min(len(pages), 2)))
            steps = ["wiki_orient", "wiki_search", "wiki_read", "wiki_follow_links", "wiki_sufficiency_check"]
            reason = "Query asks for synthesis, comparison, or relationship across Wiki pages."
        elif browse_intent:
            strategy = "browse_first"
            fan_in = min(3, max(1, len(pages)))
            steps = ["wiki_orient", "wiki_browse", "wiki_read", "wiki_sufficiency_check"]
            reason = "Query asks for overview, directory, schema, or inventory-style knowledge."
        elif not hits:
            strategy = "insufficient"
            fan_in = 0
            steps = ["wiki_orient", "wiki_search", "wiki_sufficiency_check"]
            reason = "No page-level Wiki evidence matched the query."
        else:
            strategy = "search_first"
            fan_in = 1
            steps = ["wiki_orient", "wiki_search", "wiki_read", "wiki_sufficiency_check"]
            reason = "Query is focused enough for page-index search followed by page read."

        open_constraints = self._constraints_for_query(db, query, hits)
        if strategy != "insufficient" and hits and any(item.get("error_type") in HIGH_RISK_REPAIR_TYPES for item in open_constraints):
            steps.append("wiki_follow_links")
            reason += " Open Error Book constraints should be considered before relying on the result."
        return {
            "query": query,
            "strategy": strategy,
            "required_fan_in": fan_in,
            "recommended_steps": steps,
            "reason": reason,
            "items": [
                {
                    "page_key": item["page_key"],
                    "title": item["title"],
                    "summary": item["summary"],
                    "score": item["score"],
                    "match_reasons": item.get("match_reasons", []),
                    "next_actions": item.get("next_actions", []),
                }
                for item in hits
            ],
            "open_constraints": open_constraints,
        }

    def browse(
        self,
        db: Session,
        *,
        path_prefix: str = "",
        page_type: str = "",
        limit: int = 100,
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 500))
        normalized_prefix = path_prefix.strip().strip("/")
        expected_type = page_type.strip()
        indexed = self._indexed_page_keys()
        link_counts = self._link_counts(db)
        pages = []
        for page in self.list_pages(db, limit=1000):
            path = (page.path or "").strip("/")
            if normalized_prefix and not (path.startswith(normalized_prefix) or page.page_key.startswith(normalized_prefix)):
                continue
            if expected_type and page.page_type != expected_type:
                continue
            inbound, outbound = link_counts.get(page.page_key, (0, 0))
            pages.append(
                {
                    "page_key": page.page_key,
                    "title": page.title,
                    "path": page.path,
                    "summary": page.summary,
                    "tags": page.tags or [],
                    "page_type": page.page_type,
                    "confidence": _confidence(page.confidence),
                    "is_index_linked": page.page_key in indexed or page.page_key == "index",
                    "inbound_links": inbound,
                    "outbound_links": outbound,
                    "source": "wiki_browse",
                    "next_actions": ["wiki_read", "wiki_follow_links"],
                }
            )
        pages.sort(key=lambda item: (not item["is_index_linked"], item["path"], item["page_key"]))
        return {
            "path_prefix": normalized_prefix,
            "page_type": expected_type,
            "items": pages[:limit],
            "total": len(pages),
            "next_actions": ["wiki_read selected pages", "wiki_sufficiency_check before answering"],
        }

    def sufficiency_check(
        self,
        db: Session,
        *,
        claim: str,
        read_pages: list[str],
        required_fan_in: int | None = None,
        strategy: str = "",
    ) -> dict[str, Any]:
        normalized_pages = [normalize_page_key(item) for item in read_pages if str(item).strip()]
        route = self.route(db, claim, limit=10) if required_fan_in is None else {}
        fan_in = required_fan_in if required_fan_in is not None else int(route.get("required_fan_in") or 1)
        fan_in = max(0, fan_in)
        pages = [page for key in normalized_pages if (page := self.read(db, key)) is not None]
        found_keys = {page.page_key for page in pages}
        missing_keys = [key for key in normalized_pages if key not in found_keys]
        coverage = self._claim_coverage(claim, pages)
        constraints = self._constraints_for_pages(db, normalized_pages)
        evidence_gaps: list[dict[str, Any]] = []
        if not normalized_pages:
            evidence_gaps.append({"type": "no_read_pages", "message": "No wiki_read page bodies were provided."})
        if missing_keys:
            evidence_gaps.append({"type": "missing_pages", "pages": missing_keys})
        if fan_in and len(found_keys) < fan_in:
            evidence_gaps.append({"type": "fan_in", "required": fan_in, "actual": len(found_keys)})
        if claim.strip() and coverage["ratio"] < 0.35 and found_keys:
            evidence_gaps.append({"type": "claim_terms_missing", "missing_terms": coverage["missing_terms"][:8]})
        if constraints:
            evidence_gaps.append(
                {
                    "type": "open_constraints",
                    "constraints": [
                        {
                            "error_type": item["error_type"],
                            "page_key": item["page_key"],
                            "constraint_rule": item.get("constraint_rule") or item.get("constraint"),
                        }
                        for item in constraints
                    ],
                }
            )
        sufficient = not evidence_gaps
        return {
            "sufficient": sufficient,
            "claim": claim,
            "strategy": strategy or str(route.get("strategy") or ""),
            "required_fan_in": fan_in,
            "read_pages": normalized_pages,
            "evidence_pages": [
                {
                    "page_key": page.page_key,
                    "title": page.title,
                    "summary": page.summary,
                    "confidence": _confidence(page.confidence),
                }
                for page in pages
            ],
            "coverage": coverage,
            "open_constraints": constraints,
            "evidence_gaps": evidence_gaps,
            "requirement": "Use wiki_read page bodies, enough fan-in, and no blocking Error Book constraints before making Wiki-backed claims.",
            "next_action": ""
            if sufficient
            else "Read additional pages, follow links, repair Error Book constraints, or state that Wiki evidence is insufficient.",
        }

    def follow_links(
        self,
        db: Session,
        page_key: str,
        *,
        direction: str = "out",
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        key = normalize_page_key(page_key)
        limit = max(1, min(limit, 200))
        if direction not in {"out", "in", "both"}:
            raise ValueError("direction must be out, in, or both")
        clauses = []
        if direction in {"out", "both"}:
            clauses.append(WikiLink.src_page_key == key)
        if direction in {"in", "both"}:
            clauses.append(WikiLink.dst_page_key == key)
        links = list(
            db.scalars(
                select(WikiLink)
                .where(or_(*clauses), WikiLink.status == "resolved")
                .order_by(WikiLink.created_at.desc())
                .limit(limit)
            ).all()
        )
        page_keys = {link.dst_page_key if link.src_page_key == key else link.src_page_key for link in links}
        pages = {
            page.page_key: page
            for page in db.scalars(select(WikiPage).where(WikiPage.page_key.in_(page_keys))).all()
        } if page_keys else {}
        return [
            {
                "src_page_key": link.src_page_key,
                "dst_page_key": link.dst_page_key,
                "anchor_text": link.anchor_text,
                "direction": "out" if link.src_page_key == key else "in",
                "page": self._page_summary(pages.get(link.dst_page_key if link.src_page_key == key else link.src_page_key)),
            }
            for link in links
        ]

    def lint(self, db: Session) -> dict[str, Any]:
        pages = self.scan()
        known = {page.page_key for page in pages}
        canonical_missing = [
            filename
            for filename in CANONICAL_WIKI_FILES
            if not (self.root / filename).exists()
        ]
        indexed_keys = self._indexed_page_keys()
        errors: list[WikiErrorBook] = []
        stale_types = {
            "broken_link",
            "orphan_page",
            "missing_index_entry",
            "low_confidence",
            "contested_claim",
            "claim_low_confidence",
            "claim_contradiction",
            "claim_missing_evidence",
            "missing_canonical_file",
            "stale_page",
            "source_drift",
        }
        now = datetime.now(UTC)
        for old_error in db.scalars(select(WikiErrorBook).where(WikiErrorBook.error_type.in_(sorted(stale_types)), WikiErrorBook.status == "open")):
            old_error.status = "fixed"
            old_error.lifecycle_status = "fixed"
            old_error.fixed_at = now
        for filename in canonical_missing:
            errors.append(self._error("missing_canonical_file", filename, f"Missing {filename}.", "Create the canonical LLM-Wiki control file.", {"path": filename}))
        linked_targets = {target for page in pages for target, _anchor in page.links if target in known}
        for page in pages:
            if page.relative_path.startswith("_archive/"):
                continue
            for target, anchor in page.links:
                if target not in known:
                    errors.append(
                        self._error(
                            "broken_link",
                            page.page_key,
                            f"Unresolved wiki link: {target}",
                            "Create the target page or fix the wikilink.",
                            {"target": target, "anchor": anchor},
                        )
                    )
            if page.page_key not in indexed_keys and Path(page.relative_path).name not in CANONICAL_WIKI_FILES:
                errors.append(
                    self._error(
                        "missing_index_entry",
                        page.page_key,
                        "Page is not referenced in index.md.",
                        "Add this page to index.md with a one-line summary.",
                        {"path": page.relative_path},
                    )
                )
            confidence = _confidence(page.confidence)
            if confidence < 0.35:
                errors.append(
                    self._error(
                        "low_confidence",
                        page.page_key,
                        f"Page confidence is low: {confidence:.2f}",
                        "Verify sources or mark the claim as contested.",
                        {"confidence": confidence},
                    )
                )
            if bool(page.metadata.get("contested", False)):
                errors.append(
                    self._error(
                        "contested_claim",
                        page.page_key,
                        "Page is marked contested.",
                        "Resolve the contested claim or keep explicit evidence in the page.",
                        {"path": page.relative_path},
                    )
                )
            if page.stale_after is not None:
                stale_after = page.stale_after if page.stale_after.tzinfo else page.stale_after.replace(tzinfo=UTC)
                if stale_after < now:
                    errors.append(
                        self._error(
                            "stale_page",
                            page.page_key,
                            "Page stale_after has passed.",
                            "Review the page and refresh its evidence or stale_after metadata.",
                            {"stale_after": stale_after.isoformat(), "path": page.relative_path},
                        )
                    )
            errors.extend(self._claim_quality_errors(page))
            if (
                page.page_key not in linked_targets
                and page.page_key not in {"index", "schema", "log"}
                and Path(page.relative_path).name not in CANONICAL_WIKI_FILES
            ):
                errors.append(
                    self._error(
                        "orphan_page",
                        page.page_key,
                        "Page has no inbound wikilinks.",
                        "Link it from index.md or a relevant entity/concept page.",
                        {"path": page.relative_path},
                    )
                )
            source_paths = _listify(page.metadata.get("sources"))
            for source in source_paths:
                if source.startswith("raw/") and not (self.root / source).exists():
                    errors.append(
                        self._error(
                            "source_drift",
                            page.page_key,
                            f"Declared source is missing: {source}",
                            "Restore the raw source or update frontmatter sources.",
                            {"source": source},
                        )
                    )
        for error in errors:
            db.add(error)
        if self.events:
            self.events.emit("wiki.lint", {"errors": len(errors), "canonical_missing": canonical_missing})
        quality = self.quality_summary(db, parsed_pages=pages, errors=errors)
        return {
            "ok": not errors,
            "errors": len(errors),
            "canonical_missing": canonical_missing,
            "quality": quality,
            "items": [
                {
                    "error_type": error.error_type,
                    "page_key": error.page_key,
                    "root_cause": error.root_cause,
                    "constraint": error.constraint,
                    "payload": error.payload,
                }
                for error in errors
            ],
        }

    def repair(
        self,
        db: Session,
        *,
        apply_safe: bool = True,
        error_ids: list[str] | None = None,
        llm: Any | None = None,
        actor: str = "wiki_repair",
    ) -> dict[str, Any]:
        lint_result = self.lint(db)
        db.flush()
        selected_ids = {str(item) for item in (error_ids or []) if str(item).strip()}
        open_errors = self.error_book(db, status="open", limit=500)
        if selected_ids:
            open_errors = [item for item in open_errors if str(item.id) in selected_ids]
        created: list[dict[str, Any]] = []
        applied: list[dict[str, Any]] = []
        skipped: list[dict[str, Any]] = []
        seen_low_risk_targets: set[tuple[str, str]] = set()

        for error in open_errors:
            if error.status != "open":
                continue
            error_type = str(error.error_type)
            if error_type in LOW_RISK_REPAIR_TYPES:
                proposal = self._low_risk_repair_proposal(error)
                if proposal is None:
                    skipped.append({"error_id": str(error.id), "error_type": error_type, "reason": "no deterministic repair"})
                    continue
                key = (proposal["action"], str(proposal["payload"].get("page_key") or proposal["payload"].get("path") or ""))
                if key in seen_low_risk_targets:
                    skipped.append({"error_id": str(error.id), "error_type": error_type, "reason": "duplicate safe repair"})
                    continue
                seen_low_risk_targets.add(key)
                existing = self._find_existing_repair_proposal(db, error)
                if existing is not None:
                    skipped.append(
                        {
                            "error_id": str(error.id),
                            "error_type": error_type,
                            "reason": "existing repair proposal",
                            "proposal_id": str(existing.id),
                        }
                    )
                    continue
                record = self._create_repair_proposal(db, error, proposal, risk_level="low")
                created.append({"proposal_id": str(record.id), "error_id": str(error.id), "risk_level": "low"})
                if apply_safe:
                    applied_record = self.apply_proposal(db, record.id, actor=actor)
                    error.status = "fixed"
                    error.lifecycle_status = "fixed"
                    error.fixed_at = datetime.now(UTC)
                    applied.append(
                        {
                            "proposal_id": str(applied_record.id),
                            "action": applied_record.action,
                            "result": applied_record.result,
                        }
                    )
                continue

            if error_type in HIGH_RISK_REPAIR_TYPES or error_type:
                existing = self._find_existing_repair_proposal(db, error)
                if existing is not None:
                    skipped.append(
                        {
                            "error_id": str(error.id),
                            "error_type": error_type,
                            "reason": "existing repair proposal",
                            "proposal_id": str(existing.id),
                        }
                    )
                    continue
                diagnosis = self._diagnose_high_risk_error(error, llm=llm)
                record = self._create_repair_proposal(
                    db,
                    error,
                    {
                        "action": "append_log",
                        "payload": {
                            "page_key": normalize_page_key(error.page_key),
                            "error_id": str(error.id),
                            "entry": (
                                f"Review Wiki Error Book item `{error_type}` for `{error.page_key}`: "
                                f"{diagnosis.get('root_cause') or error.root_cause}"
                            ),
                            "repair_kind": "semantic_review",
                            "diagnosis": diagnosis,
                        },
                    },
                    risk_level="medium",
                    extra_evidence={"diagnosis": diagnosis},
                )
                created.append({"proposal_id": str(record.id), "error_id": str(error.id), "risk_level": "medium"})

        if self.events:
            self.events.emit(
                "wiki.repair",
                {
                    "errors_seen": len(open_errors),
                    "proposals_created": len(created),
                    "proposals_applied": len(applied),
                    "lint_errors": lint_result.get("errors", 0),
                },
                severity="info",
            )
        return {
            "ok": True,
            "errors_seen": len(open_errors),
            "lint": lint_result,
            "proposals_created": len(created),
            "proposals_applied": len(applied),
            "created": created,
            "applied": applied,
            "skipped": skipped,
        }

    def apply_proposal(self, db: Session, proposal_id: uuid.UUID, *, actor: str = "admin") -> EvolutionProposal:
        proposal = db.get(EvolutionProposal, proposal_id)
        if proposal is None:
            raise KeyError(f"proposal not found: {proposal_id}")
        if proposal.status not in {"pending", "approved"}:
            raise ValueError(f"proposal is not applyable: {proposal.status}")
        if proposal.target_type != "wiki":
            raise ValueError("only wiki proposals are applyable here")
        if proposal.action not in WIKI_PROPOSAL_ACTIONS:
            raise ValueError(f"unsupported wiki proposal action: {proposal.action}")
        before: dict[str, Any] = {}
        payload = proposal.payload or {}
        if proposal.action in {"create_page", "update_page", "fix_link", "update_index"}:
            path = self._proposal_path(payload, default="index.md" if proposal.action == "update_index" else "")
            before = {"path": path.as_posix(), "content": self._read_path(path)}
            self._write_path(path, str(payload.get("content") or ""))
            changed = path.as_posix()
        elif proposal.action == "archive_page":
            path = self._proposal_path(payload)
            before = {"path": path.as_posix(), "content": self._read_path(path)}
            archive_path = self.root / "_archive" / path.name
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            path.rename(archive_path)
            changed = archive_path.relative_to(self.root).as_posix()
        else:
            log_path = Path("log.md")
            before = {"path": "log.md", "content": self._read_path(log_path)}
            self._append_log(str(payload.get("entry") or ""), actor=actor)
            changed = "log.md"
        self._append_log(f"Applied wiki proposal {proposal.id}: {proposal.action} {changed}", actor=actor)
        compile_result = self.compile(db)
        proposal.status = "applied"
        proposal.before_snapshot = before
        proposal.after_snapshot = {"changed": changed, "compile": compile_result}
        proposal.result = {"ok": True, "action": proposal.action, "changed": changed, "actor": actor}
        proposal.applied_at = datetime.now(UTC)
        if self.events:
            self.events.emit("wiki.proposal.applied", {"proposal_id": str(proposal.id), "action": proposal.action})
            self.events.audit(
                "wiki.proposal.apply",
                "evolution_proposal",
                target_id=str(proposal.id),
                payload={"action": proposal.action, "changed": changed, "actor": actor},
            )
        return proposal

    def error_book(self, db: Session, status: str | None = "open", limit: int = 100) -> list[WikiErrorBook]:
        limit = max(1, min(limit, 500))
        stmt = select(WikiErrorBook).order_by(desc(WikiErrorBook.created_at)).limit(limit)
        if status:
            stmt = stmt.where(WikiErrorBook.status == status)
        return list(db.scalars(stmt).all())

    def health(self, db: Session) -> dict[str, Any]:
        page_count = db.scalar(select(func.count()).select_from(WikiPage)) or 0
        open_errors = db.scalar(
            select(func.count()).select_from(WikiErrorBook).where(WikiErrorBook.status == "open")
        ) or 0
        last_run = db.scalar(select(WikiCompileRun).order_by(desc(WikiCompileRun.started_at)).limit(1))
        return {
            "root": str(self.root),
            "pages": page_count,
            "open_errors": open_errors,
            "last_compile": None
            if last_run is None
            else {
                "status": last_run.status,
                "pages_indexed": last_run.pages_indexed,
                "errors": last_run.errors,
                "started_at": last_run.started_at.isoformat() if last_run.started_at else None,
                "finished_at": last_run.finished_at.isoformat() if last_run.finished_at else None,
            },
            "llm_wiki": self._wiki_shape(),
            "fts": self.fts_status(db),
        }

    def quality_summary(
        self,
        db: Session,
        *,
        parsed_pages: list[ParsedWikiPage] | None = None,
        errors: list[WikiErrorBook] | None = None,
    ) -> dict[str, Any]:
        if parsed_pages is None:
            pages = self.list_pages(db, limit=1000)
            total_claims = sum(len(getattr(page, "claims", []) or []) for page in pages)
            pages_with_evidence = sum(1 for page in pages if getattr(page, "source_refs", []) or (page.metadata_json or {}).get("sources"))
            stale_pages = sum(1 for page in pages if getattr(page, "stale_after", None) and self._is_past(page.stale_after))
            low_confidence_pages = sum(1 for page in pages if _confidence(getattr(page, "confidence", 0.0)) < 0.35)
        else:
            total_claims = sum(len(page.claims) for page in parsed_pages)
            pages_with_evidence = sum(1 for page in parsed_pages if page.source_refs or page.metadata.get("sources"))
            stale_pages = sum(1 for page in parsed_pages if page.stale_after is not None and self._is_past(page.stale_after))
            low_confidence_pages = sum(1 for page in parsed_pages if _confidence(page.confidence) < 0.35)
            pages = parsed_pages
        if errors is None:
            open_errors = self.error_book(db, status="open", limit=500)
        else:
            open_errors = errors
        quality_error_types = {"low_confidence", "claim_low_confidence", "claim_contradiction", "claim_missing_evidence", "stale_page", "source_drift"}
        quality_errors = [item for item in open_errors if item.error_type in quality_error_types]
        page_count = len(pages)
        return {
            "pages": page_count,
            "claims": total_claims,
            "pages_with_evidence": pages_with_evidence,
            "evidence_coverage": round(pages_with_evidence / page_count, 4) if page_count else 0.0,
            "low_confidence_pages": low_confidence_pages,
            "stale_pages": stale_pages,
            "quality_errors": len(quality_errors),
        }

    def refresh_fts(self, db: Session, pages: list[ParsedWikiPage] | None = None) -> dict[str, Any]:
        backend = self._db_backend(db)
        if backend == "sqlite":
            return self._refresh_sqlite_fts(db, pages or self.scan())
        if backend == "postgresql":
            return self._postgres_fts_status(db)
        return {"backend": backend, "available": False, "indexed_pages": 0, "fallback": True}

    def fts_status(self, db: Session) -> dict[str, Any]:
        backend = self._db_backend(db)
        if backend == "sqlite":
            try:
                result = db.execute(
                    text("SELECT count(*) FROM sqlite_master WHERE type IN ('table', 'virtual table') AND name = 'wiki_pages_fts'")
                )
                exists = int(_result_scalar(result, 0) or 0) > 0
                indexed = 0
                if exists:
                    indexed = int(_result_scalar(db.execute(text("SELECT count(*) FROM wiki_pages_fts")), 0) or 0)
                return {"backend": "sqlite", "available": exists, "indexed_pages": indexed, "fallback": not exists}
            except Exception as exc:  # noqa: BLE001
                return {"backend": "sqlite", "available": False, "indexed_pages": 0, "fallback": True, "error": str(exc)}
        if backend == "postgresql":
            return self._postgres_fts_status(db)
        return {"backend": backend, "available": False, "indexed_pages": 0, "fallback": True}

    def _db_backend(self, db: Session) -> str:
        try:
            bind = db.get_bind()
        except Exception:  # noqa: BLE001
            bind = getattr(db, "bind", None)
        dialect = getattr(getattr(bind, "dialect", None), "name", "")
        return str(dialect or "unknown")

    def _refresh_sqlite_fts(self, db: Session, pages: list[ParsedWikiPage]) -> dict[str, Any]:
        try:
            db.execute(
                text(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS wiki_pages_fts USING fts5(
                        page_key UNINDEXED,
                        title,
                        summary,
                        body,
                        aliases,
                        tags,
                        path UNINDEXED,
                        page_type UNINDEXED
                    )
                    """
                )
            )
            db.execute(text("DELETE FROM wiki_pages_fts"))
            insert_sql = text(
                """
                INSERT INTO wiki_pages_fts(page_key, title, summary, body, aliases, tags, path, page_type)
                VALUES (:page_key, :title, :summary, :body, :aliases, :tags, :path, :page_type)
                """
            )
            for page in pages:
                db.execute(
                    insert_sql,
                    {
                        "page_key": page.page_key,
                        "title": page.title,
                        "summary": page.summary,
                        "body": page.body,
                        "aliases": " ".join(page.aliases),
                        "tags": " ".join(page.tags),
                        "path": page.relative_path,
                        "page_type": page.page_type,
                    },
                )
            return {"backend": "sqlite", "available": True, "indexed_pages": len(pages), "fallback": False}
        except Exception as exc:  # noqa: BLE001
            if self.events:
                self.events.emit("wiki.fts.refresh_failed", {"backend": "sqlite", "error": str(exc)}, severity="warning")
            return {"backend": "sqlite", "available": False, "indexed_pages": 0, "fallback": True, "error": str(exc)}

    def _postgres_fts_status(self, db: Session) -> dict[str, Any]:
        try:
            exists = bool(_result_scalar(db.execute(text("SELECT to_regclass('ix_wiki_pages_fts')")), None))
            indexed = int(_result_scalar(db.execute(text("SELECT count(*) FROM wiki_pages")), 0) or 0) if exists else 0
            return {"backend": "postgresql", "available": exists, "indexed_pages": indexed, "fallback": not exists}
        except Exception as exc:  # noqa: BLE001
            return {"backend": "postgresql", "available": False, "indexed_pages": 0, "fallback": True, "error": str(exc)}

    def _fts_search(self, db: Session, query: str, *, limit: int = 20) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        backend = self._db_backend(db)
        if backend == "sqlite":
            return self._sqlite_fts_search(db, query, limit=limit)
        if backend == "postgresql":
            return self._postgres_fts_search(db, query, limit=limit)
        return []

    def _sqlite_fts_search(self, db: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        fts_query = _safe_fts_query(query)
        if not fts_query:
            return []
        try:
            sql = text(
                """
                SELECT
                    page_key,
                    bm25(wiki_pages_fts, 8.0, 4.0, 2.0, 1.0, 3.0, 2.0, 0.2, 0.5) AS raw_rank,
                    snippet(wiki_pages_fts, 3, '[', ']', ' ... ', 24) AS snippet
                FROM wiki_pages_fts
                WHERE wiki_pages_fts MATCH :query
                ORDER BY raw_rank
                LIMIT :limit
                """
            )
            rows = db.execute(sql, {"query": fts_query, "limit": limit}).mappings().all()
        except Exception:
            return []
        hits: list[dict[str, Any]] = []
        for row in rows:
            raw_rank = abs(float(row.get("raw_rank") or 0.0))
            hits.append(
                {
                    "page_key": str(row.get("page_key") or ""),
                    "rank": 1.0 / (1.0 + raw_rank),
                    "snippet": str(row.get("snippet") or ""),
                }
            )
        return [item for item in hits if item["page_key"]]

    def _postgres_fts_search(self, db: Session, query: str, *, limit: int) -> list[dict[str, Any]]:
        try:
            sql = text(
                """
                WITH q AS (SELECT plainto_tsquery('simple', :query) AS query),
                pages AS (
                    SELECT
                        page_key,
                        coalesce(body, summary, '') AS headline_text,
                        setweight(to_tsvector('simple', coalesce(title, '')), 'A') ||
                        setweight(to_tsvector('simple', coalesce(summary, '')), 'B') ||
                        setweight(
                            jsonb_to_tsvector(
                                'simple',
                                CASE WHEN jsonb_typeof(aliases) = 'array' THEN aliases ELSE '[]'::jsonb END,
                                '["string"]'::jsonb
                            ),
                            'B'
                        ) ||
                        setweight(
                            jsonb_to_tsvector(
                                'simple',
                                CASE WHEN jsonb_typeof(tags) = 'array' THEN tags ELSE '[]'::jsonb END,
                                '["string"]'::jsonb
                            ),
                            'B'
                        ) ||
                        setweight(to_tsvector('simple', coalesce(body, '')), 'C') AS document
                    FROM wiki_pages
                )
                SELECT
                    page_key,
                    ts_rank_cd(document, q.query) AS rank,
                    ts_headline('simple', headline_text, q.query, 'MaxWords=24, MinWords=8') AS snippet
                FROM pages, q
                WHERE document @@ q.query
                ORDER BY rank DESC
                LIMIT :limit
                """
            )
            rows = db.execute(sql, {"query": query, "limit": limit}).mappings().all()
        except Exception:
            return []
        return [
            {"page_key": str(row.get("page_key") or ""), "rank": float(row.get("rank") or 0.0), "snippet": str(row.get("snippet") or "")}
            for row in rows
            if row.get("page_key")
        ]

    def _candidate_pages(
        self,
        db: Session,
        *,
        query: str,
        path_prefix: str,
        page_keys: set[str],
        limit: int,
    ) -> list[WikiPage]:
        pages = self.list_pages(db, limit=max(limit, 1000))
        seen = {page.page_key for page in pages}
        missing_fts_keys = [key for key in page_keys if key and key not in seen]
        if missing_fts_keys:
            missing_set = set(missing_fts_keys)
            try:
                fts_pages = list(db.scalars(select(WikiPage).where(WikiPage.page_key.in_(missing_fts_keys))).all())
            except Exception:  # noqa: BLE001
                fts_pages = []
            for page in fts_pages:
                if page.page_key in missing_set and page.page_key not in seen:
                    pages.append(page)
                    seen.add(page.page_key)
        normalized_prefix = path_prefix.strip().strip("/")
        if normalized_prefix:
            pages = [
                page
                for page in pages
                if (page.path or "").strip("/").startswith(normalized_prefix) or page.page_key.startswith(normalized_prefix)
            ]
        if not query.strip():
            return pages[:limit]
        tokens = _query_tokens(query)
        selected: list[WikiPage] = []
        for page in pages:
            if page.page_key in page_keys or self._page_has_text_match(page, query, tokens):
                selected.append(page)
        return selected[:limit]

    def _page_has_text_match(self, page: WikiPage, query: str, tokens: list[str]) -> bool:
        fields = [
            page.page_key,
            page.title,
            page.summary,
            page.body,
            page.path,
            page.page_type,
            " ".join(page.aliases or []),
            " ".join(page.tags or []),
        ]
        return any(_field_contains(str(field or ""), query, tokens) for field in fields)

    def _score_page(
        self,
        page: WikiPage,
        *,
        query: str,
        tokens: list[str],
        fts_rank: float,
        link_counts: dict[str, tuple[int, int]],
    ) -> tuple[float, list[str], list[str]]:
        if not query.strip():
            return (_confidence(page.confidence), [], ["recent_or_index_listing"])
        normalized_query = normalize_page_key(query)
        lower_query = query.lower()
        aliases = [str(item) for item in (page.aliases or [])]
        tags = [str(item) for item in (page.tags or [])]
        field_values = {
            "page_key": page.page_key,
            "title": page.title,
            "summary": page.summary,
            "body": page.body,
            "path": page.path,
            "page_type": page.page_type,
            "aliases": " ".join(aliases),
            "tags": " ".join(tags),
        }
        score = 0.0
        matched_fields: list[str] = []
        reasons: list[str] = []
        if normalized_query and page.page_key == normalized_query:
            score += 9.0
            matched_fields.append("page_key")
            reasons.append("exact_page_key")
        if lower_query and page.title.lower() == lower_query:
            score += 8.0
            matched_fields.append("title")
            reasons.append("exact_title")
        if any(normalize_page_key(alias) == normalized_query for alias in aliases):
            score += 7.0
            matched_fields.append("aliases")
            reasons.append("exact_alias")
        if any(tag.lower() == lower_query for tag in tags):
            score += 5.5
            matched_fields.append("tags")
            reasons.append("exact_tag")
        weights = {
            "page_key": 4.0,
            "title": 3.2,
            "aliases": 2.8,
            "tags": 2.4,
            "path": 1.6,
            "summary": 1.4,
            "page_type": 1.0,
            "body": 0.8,
        }
        for field, value in field_values.items():
            text_value = str(value or "")
            if _field_contains(text_value, query, tokens):
                if field not in matched_fields:
                    matched_fields.append(field)
                score += weights[field]
                reasons.append(f"{field}_match")
        if fts_rank:
            score += min(3.0, fts_rank * 3.0)
            reasons.append("fts_match")
        inbound, outbound = link_counts.get(page.page_key, (0, 0))
        if inbound:
            score += min(1.0, inbound * 0.2)
            reasons.append("backlinked")
        if outbound:
            score += min(0.5, outbound * 0.1)
        score += _confidence(page.confidence)
        return round(score, 4), sorted(set(matched_fields)), sorted(set(reasons))

    def _link_counts(self, db: Session) -> dict[str, tuple[int, int]]:
        try:
            links = list(db.scalars(select(WikiLink).where(WikiLink.status == "resolved")).all())
        except Exception:  # noqa: BLE001
            return {}
        counts: dict[str, list[int]] = {}
        for link in links:
            counts.setdefault(link.dst_page_key, [0, 0])[0] += 1
            counts.setdefault(link.src_page_key, [0, 0])[1] += 1
        return {key: (value[0], value[1]) for key, value in counts.items()}

    def _page_snippet(self, page: WikiPage, query: str) -> str:
        return _snippet(page.summary, query) or _snippet(page.body, query) or page.summary[:260]

    def _next_actions_for_search_hit(self, page: WikiPage, *, strategy: str, constraints: list[dict[str, Any]]) -> list[str]:
        actions = ["wiki_read"]
        if strategy == "bridge" or constraints:
            actions.append("wiki_follow_links")
        if _confidence(page.confidence) < 0.5 or constraints:
            actions.append("wiki_sufficiency_check")
        return actions

    def _find_existing_repair_proposal(self, db: Session, error: WikiErrorBook) -> EvolutionProposal | None:
        error_id = str(error.id)
        try:
            proposals = list(
                db.scalars(
                    select(EvolutionProposal).where(
                        EvolutionProposal.target_type == "wiki",
                        EvolutionProposal.status.in_(("pending", "approved", "applied")),
                    )
                ).all()
            )
        except Exception:  # noqa: BLE001
            return None
        for proposal in proposals:
            payload = proposal.payload or {}
            evidence = proposal.evidence or {}
            evidence_error = evidence.get("error") if isinstance(evidence.get("error"), dict) else {}
            if str(payload.get("error_id") or "") == error_id or str(evidence_error.get("id") or "") == error_id:
                return proposal
        return None

    def _constraints_for_query(self, db: Session, query: str, hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
        page_keys = [str(item.get("page_key") or "") for item in hits]
        tokens = _query_tokens(query)
        constraints = []
        for item in self.error_book(db, status="open", limit=100):
            haystack = " ".join(
                [
                    item.page_key,
                    item.error_type,
                    item.root_cause,
                    item.constraint,
                    item.constraint_rule,
                    json.dumps(item.payload or {}, ensure_ascii=False),
                ]
            ).lower()
            if item.page_key in page_keys or any(token in haystack for token in tokens):
                constraints.append(self._error_summary(item))
        return constraints[:20]

    def _constraints_for_pages(self, db: Session, page_keys: list[str]) -> list[dict[str, Any]]:
        keys = {normalize_page_key(str(key)) for key in page_keys if str(key).strip()}
        if not keys:
            return []
        constraints = []
        for item in self.error_book(db, status="open", limit=200):
            if normalize_page_key(item.page_key) in keys:
                constraints.append(self._error_summary(item))
        return constraints

    def _claim_coverage(self, claim: str, pages: list[WikiPage]) -> dict[str, Any]:
        tokens = _query_tokens(claim)
        if not tokens:
            return {"ratio": 1.0, "matched_terms": [], "missing_terms": []}
        haystack = "\n".join(
            f"{page.page_key} {page.title} {page.summary} {page.body} {' '.join(page.aliases or [])} {' '.join(page.tags or [])}"
            for page in pages
        ).lower()
        matched = [token for token in tokens if token in haystack]
        missing = [token for token in tokens if token not in haystack]
        return {"ratio": round(len(matched) / max(1, len(tokens)), 4), "matched_terms": matched, "missing_terms": missing}

    def _low_risk_repair_proposal(self, error: WikiErrorBook) -> dict[str, Any] | None:
        error_type = error.error_type
        payload = error.payload or {}
        if error_type == "missing_canonical_file":
            path = str(payload.get("path") or error.page_key)
            if path not in CANONICAL_WIKI_FILES:
                return None
            return {
                "action": "create_page",
                "payload": {
                    "path": path,
                    "content": DEFAULT_CANONICAL_CONTENT[path],
                    "repair_kind": "create_canonical_file",
                    "error_id": str(error.id),
                },
            }
        if error_type in {"missing_index_entry", "orphan_page"}:
            page_key = normalize_page_key(error.page_key)
            if not page_key:
                return None
            content = self._index_content_with_link(page_key)
            return {
                "action": "update_index",
                "payload": {
                    "path": "index.md",
                    "page_key": page_key,
                    "content": content,
                    "repair_kind": "index_link",
                    "error_id": str(error.id),
                },
            }
        return None

    def _index_content_with_link(self, page_key: str) -> str:
        current = self._read_optional_file("index.md")
        if not current.strip():
            current = DEFAULT_CANONICAL_CONTENT["index.md"]
        if f"[[{page_key}]]" in current:
            return current
        label = page_key.split("/")[-1].replace("-", " ").title()
        return current.rstrip() + f"\n\n- [[{page_key}]] {label}\n"

    def _create_repair_proposal(
        self,
        db: Session,
        error: WikiErrorBook,
        proposal: dict[str, Any],
        *,
        risk_level: str,
        extra_evidence: dict[str, Any] | None = None,
    ) -> EvolutionProposal:
        record = EvolutionProposal(
            target_type="wiki",
            action=str(proposal["action"]),
            status="pending",
            risk_level=risk_level,
            payload=proposal.get("payload") if isinstance(proposal.get("payload"), dict) else {},
            evidence={
                "source": "wiki_repair",
                "error": self._error_summary(error),
                **(extra_evidence or {}),
            },
        )
        db.add(record)
        db.flush()
        if self.events:
            self.events.emit(
                "wiki.repair.proposal_created",
                {"proposal_id": str(record.id), "error_id": str(error.id), "risk_level": risk_level},
            )
        return record

    def _diagnose_high_risk_error(self, error: WikiErrorBook, *, llm: Any | None = None) -> dict[str, Any]:
        fallback = {
            "root_cause": error.root_cause,
            "constraint_rule": error.constraint_rule or error.constraint,
            "verification_method": error.verification_method or "Run wiki lint/compile and answer a retrieval probe before trusting this page.",
            "source_refs": error.source_refs or [],
            "proposed_action": "review_before_apply",
        }
        if llm is None or not bool(getattr(llm, "configured", False)):
            return fallback
        try:
            response = llm.complete(
                messages=[
                    {
                        "role": "system",
                        "content": "Diagnose one LLM-Wiki Error Book item. Return compact JSON only.",
                    },
                    {
                        "role": "user",
                        "content": json.dumps(
                            {
                                "error_type": error.error_type,
                                "page_key": error.page_key,
                                "root_cause": error.root_cause,
                                "constraint": error.constraint,
                                "payload": error.payload or {},
                            },
                            ensure_ascii=False,
                        ),
                    },
                ],
                tools=None,
                temperature=0.1,
            )
            parsed = json.loads(response.content)
            if isinstance(parsed, dict):
                return {**fallback, **parsed}
        except Exception as exc:  # noqa: BLE001
            fallback["llm_error"] = str(exc)
        return fallback

    def _claim_quality_errors(self, page: ParsedWikiPage) -> list[WikiErrorBook]:
        errors: list[WikiErrorBook] = []
        page_sources = page.source_refs or [{"ref": item} for item in _listify(page.metadata.get("sources"))]
        seen_claims: dict[str, dict[str, Any]] = {}
        for index, claim in enumerate(page.claims):
            text_value = str(claim.get("text") or claim.get("claim") or "").strip()
            claim_id = str(claim.get("id") or f"claim-{index + 1}")
            confidence = _confidence(claim.get("confidence", page.confidence))
            evidence = claim.get("evidence") or claim.get("source_refs") or []
            if isinstance(evidence, str):
                evidence = [evidence]
            if confidence < 0.35:
                errors.append(
                    self._error(
                        "claim_low_confidence",
                        page.page_key,
                        f"Claim `{claim_id}` confidence is low: {confidence:.2f}",
                        "Verify the claim with explicit evidence or mark it contested.",
                        {"claim_id": claim_id, "confidence": confidence, "claim": text_value},
                    )
                )
            if not evidence and not page_sources:
                errors.append(
                    self._error(
                        "claim_missing_evidence",
                        page.page_key,
                        f"Claim `{claim_id}` has no evidence reference.",
                        "Attach source_refs/evidence at the claim or page level.",
                        {"claim_id": claim_id, "claim": text_value},
                    )
                )
            normalized = re.sub(r"\s+", " ", text_value.lower())
            polarity = str(claim.get("polarity") or claim.get("status") or "asserted").lower()
            if normalized and normalized in seen_claims:
                previous = seen_claims[normalized]
                previous_polarity = str(previous.get("polarity") or previous.get("status") or "asserted").lower()
                if {polarity, previous_polarity} & {"contested", "false", "negated"} and polarity != previous_polarity:
                    errors.append(
                        self._error(
                            "claim_contradiction",
                            page.page_key,
                            f"Claim `{claim_id}` contradicts another claim on the same page.",
                            "Resolve the contradiction or split contested claims with evidence.",
                            {"claim_id": claim_id, "claim": text_value},
                        )
                    )
            if normalized:
                seen_claims[normalized] = claim
        return errors

    @staticmethod
    def _is_past(value: datetime) -> bool:
        current = value if value.tzinfo else value.replace(tzinfo=UTC)
        return current < datetime.now(UTC)

    @staticmethod
    def _error_summary(error: WikiErrorBook) -> dict[str, Any]:
        return {
            "id": str(error.id),
            "error_type": error.error_type,
            "page_key": error.page_key,
            "root_cause": error.root_cause,
            "constraint": error.constraint,
            "constraint_rule": getattr(error, "constraint_rule", "") or "",
            "verification_method": getattr(error, "verification_method", "") or "",
            "source_refs": getattr(error, "source_refs", []) or [],
            "payload": error.payload or {},
        }

    def _markdown_paths(self) -> list[Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for path in sorted(self.root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            paths.append(path)
        return paths

    def _read_optional_file(self, relative_path: str) -> str:
        path = self.root / relative_path
        if not path.exists() or not path.is_file():
            return ""
        return path.read_text(encoding="utf-8")

    def _wiki_shape(self) -> dict[str, Any]:
        return {
            "canonical_files": {filename: (self.root / filename).exists() for filename in sorted(CANONICAL_WIKI_FILES)},
            "special_dirs": {dirname: (self.root / dirname).exists() for dirname in sorted(SPECIAL_WIKI_DIRS)},
        }

    def _indexed_page_keys(self) -> set[str]:
        return {
            normalize_page_key(match.group(1))
            for match in WIKILINK_RE.finditer(self._read_optional_file("index.md"))
        }

    @staticmethod
    def _page_summary(page: WikiPage | None) -> dict[str, Any] | None:
        if page is None:
            return None
        return {
            "page_key": page.page_key,
            "title": page.title,
            "page_type": page.page_type,
            "path": page.path,
            "summary": page.summary,
        }

    @staticmethod
    def _error(error_type: str, page_key: str, root_cause: str, constraint: str, payload: dict[str, Any]) -> WikiErrorBook:
        return WikiErrorBook(
            error_type=error_type,
            page_key=page_key,
            root_cause=root_cause,
            constraint=constraint,
            constraint_rule=payload.get("constraint_rule") or constraint,
            verification_method=payload.get("verification_method") or "Run wiki lint/compile and answer the associated retrieval probe.",
            source_refs=payload.get("source_refs") if isinstance(payload.get("source_refs"), list) else [],
            lifecycle_status="open",
            status="open",
            payload=payload,
        )

    def _proposal_path(self, payload: dict[str, Any], *, default: str = "") -> Path:
        raw = str(payload.get("path") or default)
        if not raw:
            page_key = normalize_page_key(str(payload.get("page_key") or ""))
            raw = f"{page_key}.md" if page_key else ""
        path = Path(raw)
        if not raw or path.is_absolute() or ".." in path.parts:
            raise ValueError("wiki proposal requires a safe relative path")
        return path

    def _read_path(self, relative_path: Path) -> str:
        path = self.root / relative_path
        if not path.exists():
            return ""
        return path.read_text(encoding="utf-8")

    def _write_path(self, relative_path: Path, content: str) -> None:
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")

    def _append_log(self, entry: str, *, actor: str = "system") -> None:
        if not entry.strip():
            return
        path = self.root / "log.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        timestamp = datetime.now(UTC).isoformat()
        with path.open("a", encoding="utf-8") as handle:
            handle.write(f"\n- {timestamp} [{actor}] {entry.strip()}\n")
