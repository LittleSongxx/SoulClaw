"""Markdown Wiki compiler and retrieval service."""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, desc, func, or_, select
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import WikiCompileRun, WikiErrorBook, WikiLink, WikiPage, WikiSource
from backend.infra.qdrant_index import IndexDocument, QdrantHybridIndex

FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)


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
        metadata=metadata,
        checksum=_checksum(raw),
        links=links,
    )


def chunk_text(text: str, *, max_chars: int = 1200, overlap: int = 160) -> list[str]:
    text = text.strip()
    if not text:
        return []
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(len(text), start + max_chars)
        chunks.append(text[start:end].strip())
        if end >= len(text):
            break
        start = max(0, end - overlap)
    return [chunk for chunk in chunks if chunk]


class WikiService:
    def __init__(
        self,
        settings: Settings | None = None,
        qdrant: QdrantHybridIndex | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.qdrant = qdrant
        self.events = events

    @property
    def root(self) -> Path:
        return self.settings.resolved_wiki_root

    def scan(self) -> list[ParsedWikiPage]:
        self.root.mkdir(parents=True, exist_ok=True)
        pages: list[ParsedWikiPage] = []
        for path in self._markdown_paths():
            pages.append(parse_markdown_page(path, self.root))
        return pages

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
                    WikiErrorBook(
                        error_type="compile_error",
                        page_key=relative_path,
                        root_cause=str(exc),
                        constraint="Fix Markdown frontmatter/body so the compiler can parse this source.",
                        status="open",
                        payload={"path": relative_path},
                    )
                )
                continue
            if page.page_key in seen_page_keys:
                errors.append({"type": "duplicate_page_key", "path": relative_path, "page_key": page.page_key})
                new_errors.append(
                    WikiErrorBook(
                        error_type="duplicate_page_key",
                        page_key=page.page_key,
                        root_cause=f"Duplicate page_key declared by {relative_path}",
                        constraint="Give each Markdown Wiki page a unique page_key.",
                        status="open",
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

        qdrant_docs: list[IndexDocument] = []
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
                        WikiErrorBook(
                            error_type="dangling_link",
                            page_key=page.page_key,
                            root_cause=f"Unresolved wiki link: {target}",
                            constraint="Create the target page or add an alias/page_key matching the link.",
                            status="open",
                            payload={"target": target, "anchor": anchor},
                        )
                    )

            full_text = "\n\n".join(part for part in (page.title, page.summary, page.body) if part)
            for index, chunk in enumerate(chunk_text(full_text)):
                qdrant_docs.append(
                    IndexDocument(
                        key=f"{page.page_key}:chunk:{index}",
                        text=chunk,
                        payload={
                            "page_key": page.page_key,
                            "title": page.title,
                            "path": page.relative_path,
                            "chunk_index": index,
                            "tags": page.tags,
                            "page_type": page.page_type,
                        },
                    )
                )

        indexed = False
        if self.qdrant is not None:
            indexed = self.qdrant.upsert_documents(self.settings.qdrant_wiki_collection, qdrant_docs)

        run.status = "ok" if not errors else "error"
        run.pages_seen = len(source_keys)
        run.pages_indexed = len(parsed_pages)
        run.errors = len(errors)
        run.finished_at = datetime.now(UTC)
        run.payload = {
            "root": str(self.root),
            "errors": errors,
            "qdrant_indexed": indexed,
            "chunks": len(qdrant_docs),
        }
        if self.events:
            self.events.emit("wiki.compile", run.payload, severity="info" if not errors else "warning")
        return {
            "status": run.status,
            "pages_seen": run.pages_seen,
            "pages_indexed": run.pages_indexed,
            "errors": run.errors,
            "qdrant_indexed": indexed,
            "chunks": len(qdrant_docs),
        }

    def list_pages(self, db: Session, limit: int = 200) -> list[WikiPage]:
        limit = max(1, min(limit, 1000))
        stmt = select(WikiPage).order_by(WikiPage.page_key).limit(limit)
        return list(db.scalars(stmt).all())

    def read(self, db: Session, page_key: str) -> WikiPage | None:
        key = normalize_page_key(page_key)
        return db.scalar(select(WikiPage).where(WikiPage.page_key == key))

    def search(self, db: Session, query: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        query = query.strip()
        vector_hits = []
        if self.qdrant is not None and query:
            vector_hits = self.qdrant.search(self.settings.qdrant_wiki_collection, query, limit=limit)

        vector_page_keys = [
            item.get("payload", {}).get("page_key")
            for item in vector_hits
            if item.get("payload", {}).get("page_key")
        ]
        records: dict[str, dict[str, Any]] = {}
        if query:
            pattern = f"%{query}%"
            stmt = (
                select(WikiPage)
                .where(
                    or_(
                        WikiPage.title.ilike(pattern),
                        WikiPage.summary.ilike(pattern),
                        WikiPage.body.ilike(pattern),
                    )
                )
                .order_by(desc(WikiPage.updated_at))
                .limit(limit)
            )
        else:
            stmt = select(WikiPage).order_by(desc(WikiPage.updated_at)).limit(limit)
        for page in db.scalars(stmt).all():
            records[page.page_key] = {"score": 1.0, "source": "postgres", "page": page}

        if vector_page_keys:
            for page in db.scalars(select(WikiPage).where(WikiPage.page_key.in_(vector_page_keys))).all():
                hit = next(
                    (item for item in vector_hits if item.get("payload", {}).get("page_key") == page.page_key),
                    {},
                )
                records.setdefault(
                    page.page_key,
                    {"score": hit.get("score"), "source": "qdrant", "page": page},
                )

        return list(records.values())[:limit]

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
        }

    def _markdown_paths(self) -> list[Path]:
        self.root.mkdir(parents=True, exist_ok=True)
        paths: list[Path] = []
        for path in sorted(self.root.rglob("*.md")):
            if any(part.startswith(".") for part in path.relative_to(self.root).parts):
                continue
            paths.append(path)
        return paths
