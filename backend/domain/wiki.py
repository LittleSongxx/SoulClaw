"""Markdown Wiki compiler and retrieval service."""

from __future__ import annotations

import hashlib
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import yaml
from sqlalchemy import delete, desc, func, or_, select
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

FRONTMATTER_RE = re.compile(r"\A---\s*\n(?P<yaml>.*?)\n---\s*\n(?P<body>.*)\Z", re.DOTALL)
WIKILINK_RE = re.compile(r"\[\[([^\]|#]+)(?:#[^\]|]+)?(?:\|([^\]]+))?\]\]")
HEADING_RE = re.compile(r"^#\s+(.+)$", re.MULTILINE)
CANONICAL_WIKI_FILES = {"SCHEMA.md", "index.md", "log.md"}
SPECIAL_WIKI_DIRS = {"raw", "entities", "concepts", "comparisons", "queries", "_archive"}
WIKI_PROPOSAL_ACTIONS = {"create_page", "update_page", "archive_page", "fix_link", "update_index", "append_log"}


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


class WikiService:
    def __init__(
        self,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.settings = settings or get_settings()
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
            "open_constraints": open_constraints,
            "pages": [
                {
                    "page_key": page.page_key,
                    "title": page.title,
                    "page_type": page.page_type,
                    "path": page.path,
                    "summary": page.summary,
                    "tags": page.tags,
                    "confidence": page.confidence,
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

        run.status = "ok" if not errors else "error"
        run.pages_seen = len(source_keys)
        run.pages_indexed = len(parsed_pages)
        run.errors = len(errors)
        run.finished_at = datetime.now(UTC)
        run.payload = {
            "root": str(self.root),
            "errors": errors,
            "index": "page_mirror",
        }
        if self.events:
            self.events.emit("wiki.compile", run.payload, severity="info" if not errors else "warning")
        return {
            "status": run.status,
            "pages_seen": run.pages_seen,
            "pages_indexed": run.pages_indexed,
            "errors": run.errors,
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

    def search(self, db: Session, query: str, limit: int = 10) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        query = query.strip()
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

        lowered = query.lower()
        records: list[dict[str, Any]] = []
        for page in db.scalars(stmt).all():
            score = 1.0
            if lowered:
                title_or_summary = lowered in page.title.lower() or lowered in page.summary.lower()
                alias_or_tag = any(lowered in str(item).lower() for item in [*(page.aliases or []), *(page.tags or [])])
                key_hit = lowered in page.page_key.lower()
                score = 1.0 if key_hit or title_or_summary or alias_or_tag else 0.6
            records.append(
                {
                    "score": score,
                    "source": "page_index",
                    "page_key": page.page_key,
                    "title": page.title,
                    "path": page.path,
                    "summary": page.summary,
                    "tags": page.tags or [],
                    "page_type": page.page_type,
                    "confidence": page.confidence,
                    "page": page,
                }
            )

        records.sort(key=lambda item: item["score"], reverse=True)
        return records[:limit]

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
            if page.confidence < 0.35:
                errors.append(
                    self._error(
                        "low_confidence",
                        page.page_key,
                        f"Page confidence is low: {page.confidence:.2f}",
                        "Verify sources or mark the claim as contested.",
                        {"confidence": page.confidence},
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
        return {
            "ok": not errors,
            "errors": len(errors),
            "canonical_missing": canonical_missing,
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
