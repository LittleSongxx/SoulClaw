"""Knowledge embedding mirror and hybrid vector retrieval."""

from __future__ import annotations

import hashlib
import math
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import delete, desc, func, select, text
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import KnowledgeEmbedding, Memory, SkillFile, WikiPage
from backend.runtime.embedding import OpenAICompatibleEmbeddingClient


@dataclass(frozen=True)
class EmbeddingSource:
    source_type: str
    source_id: str
    chunk_key: str
    title: str
    content: str
    metadata: dict[str, Any]


class KnowledgeVectorService:
    def __init__(
        self,
        *,
        embeddings: OpenAICompatibleEmbeddingClient,
        settings: Settings | None = None,
        events: RuntimeEventBus | None = None,
    ) -> None:
        self.embeddings = embeddings
        self.settings = settings or get_settings()
        self.events = events

    def upsert_sources(self, db: Session, sources: list[EmbeddingSource], *, strict: bool | None = None) -> dict[str, Any]:
        strict = self.settings.vector_required if strict is None else strict
        valid_sources = [source for source in sources if source.content.strip()]
        if not valid_sources:
            return {"embedded": 0, "failed": 0, "skipped": len(sources), "items": []}
        batch_size = max(1, min(int(self.settings.embedding_batch_size or 32), 256))
        embedded = 0
        failed = 0
        items: list[dict[str, Any]] = []
        for start in range(0, len(valid_sources), batch_size):
            batch = valid_sources[start : start + batch_size]
            try:
                response = self.embeddings.embed_texts([source.content for source in batch])
            except Exception as exc:  # noqa: BLE001
                failed += len(batch)
                for source in batch:
                    record = self._upsert_failed(db, source, error=str(exc))
                    items.append({"id": str(record.id), "source_id": source.source_id, "status": "failed", "error": str(exc)})
                if strict:
                    raise
                continue
            for source, vector in zip(batch, response.vectors, strict=True):
                record = self._upsert_vector(
                    db,
                    source,
                    vector=vector,
                    model=response.model,
                    dimensions=response.dimensions,
                )
                embedded += 1
                items.append({"id": str(record.id), "source_id": source.source_id, "status": "embedded"})
        if self.events:
            self.events.emit("vector.embeddings.upserted", {"embedded": embedded, "failed": failed, "sources": len(valid_sources)})
        return {"embedded": embedded, "failed": failed, "skipped": len(sources) - len(valid_sources), "items": items}

    def upsert_wiki_pages(self, db: Session, pages: list[WikiPage], *, strict: bool | None = None) -> dict[str, Any]:
        return self.upsert_sources(db, [self._source_for_wiki(page) for page in pages], strict=strict)

    def upsert_memories(self, db: Session, memories: list[Memory], *, strict: bool | None = None) -> dict[str, Any]:
        return self.upsert_sources(db, [self._source_for_memory(memory) for memory in memories], strict=strict)

    def upsert_skill_files(self, db: Session, files: list[SkillFile], *, strict: bool | None = None) -> dict[str, Any]:
        return self.upsert_sources(db, [self._source_for_skill_file(file) for file in files], strict=strict)

    def mark_stale(self, db: Session, *, source_type: str, source_id: str | None = None) -> int:
        stmt = select(KnowledgeEmbedding).where(KnowledgeEmbedding.source_type == source_type)
        if source_id:
            stmt = stmt.where(KnowledgeEmbedding.source_id == source_id)
        count = 0
        for item in db.scalars(stmt).all():
            item.stale = True
            item.vector_status = "stale"
            count += 1
        if self.events and count:
            self.events.emit("vector.embeddings.marked_stale", {"source_type": source_type, "source_id": source_id or "", "count": count})
        return count

    def rebuild_all(self, db: Session, *, source_type: str = "", strict: bool | None = None) -> dict[str, Any]:
        results: dict[str, Any] = {}
        if source_type in {"", "wiki"}:
            pages = list(db.scalars(select(WikiPage).order_by(WikiPage.page_key)).all())
            results["wiki"] = self.upsert_wiki_pages(db, pages, strict=strict)
        if source_type in {"", "memory"}:
            memories = list(db.scalars(select(Memory).where(Memory.archived.is_(False)).order_by(desc(Memory.updated_at))).all())
            results["memory"] = self.upsert_memories(db, memories, strict=strict)
        if source_type in {"", "skill"}:
            files = list(db.scalars(select(SkillFile).order_by(SkillFile.skill_key, SkillFile.file_path)).all())
            results["skill"] = self.upsert_skill_files(db, files, strict=strict)
        return {"ok": True, "source_type": source_type or "all", "results": results}

    def search(
        self,
        db: Session,
        *,
        query: str,
        source_type: str,
        limit: int = 10,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 50))
        query = str(query or "").strip()
        if not query:
            return []
        response = self.embeddings.embed_texts([query])
        query_vector = response.vectors[0]
        backend = self._db_backend(db)
        if backend == "postgresql":
            try:
                return self._postgres_vector_search(db, source_type=source_type, vector=query_vector, limit=limit)
            except Exception as exc:  # noqa: BLE001
                if self.events:
                    self.events.emit("vector.search.postgres_failed", {"source_type": source_type, "error": str(exc)}, severity="warning")
        return self._python_vector_search(db, source_type=source_type, vector=query_vector, limit=limit)

    def status(self, db: Session) -> dict[str, Any]:
        rows = db.execute(
            select(KnowledgeEmbedding.source_type, KnowledgeEmbedding.vector_status, func.count()).group_by(
                KnowledgeEmbedding.source_type,
                KnowledgeEmbedding.vector_status,
            )
        ).all()
        counts: dict[str, dict[str, int]] = {}
        for source_type, status, count in rows:
            counts.setdefault(str(source_type), {})[str(status)] = int(count or 0)
        return {
            "mode": self.settings.vector_mode,
            "required": self.settings.vector_required,
            "backend": self._db_backend(db),
            "embedding_model": self.settings.embedding_model,
            "embedding_dimensions": self.settings.embedding_dimensions,
            "embedding_configured": self.embeddings.configured,
            "counts": counts,
        }

    def purge_source(self, db: Session, *, source_type: str, source_ids: set[str]) -> int:
        if not source_ids:
            result = db.execute(delete(KnowledgeEmbedding).where(KnowledgeEmbedding.source_type == source_type))
        else:
            result = db.execute(
                delete(KnowledgeEmbedding).where(
                    KnowledgeEmbedding.source_type == source_type,
                    KnowledgeEmbedding.source_id.not_in(sorted(source_ids)),
                )
            )
        return int(getattr(result, "rowcount", 0) or 0)

    def _upsert_vector(
        self,
        db: Session,
        source: EmbeddingSource,
        *,
        vector: list[float],
        model: str,
        dimensions: int,
    ) -> KnowledgeEmbedding:
        record = self._get_or_create(db, source)
        record.title = source.title
        record.content = source.content
        record.content_hash = _hash_text(source.content)
        record.embedding = vector
        record.vector_status = "embedded"
        record.model = model
        record.dimensions = dimensions
        record.stale = False
        record.metadata_json = source.metadata
        record.last_embedded_at = datetime.now(UTC)
        record.error = ""
        db.flush()
        if self._db_backend(db) == "postgresql":
            db.execute(
                text("UPDATE knowledge_embeddings SET vector_embedding = CAST(:vector AS vector) WHERE id = :id"),
                {"vector": _vector_literal(vector), "id": str(record.id)},
            )
        return record

    def _upsert_failed(self, db: Session, source: EmbeddingSource, *, error: str) -> KnowledgeEmbedding:
        record = self._get_or_create(db, source)
        record.title = source.title
        record.content = source.content
        record.content_hash = _hash_text(source.content)
        record.vector_status = "failed"
        record.model = self.settings.embedding_model
        record.dimensions = self.settings.embedding_dimensions
        record.stale = True
        record.metadata_json = source.metadata
        record.error = error[:4000]
        db.flush()
        return record

    def _get_or_create(self, db: Session, source: EmbeddingSource) -> KnowledgeEmbedding:
        record = db.scalar(
            select(KnowledgeEmbedding).where(
                KnowledgeEmbedding.source_type == source.source_type,
                KnowledgeEmbedding.source_id == source.source_id,
                KnowledgeEmbedding.chunk_key == source.chunk_key,
            )
        )
        if record is None:
            record = KnowledgeEmbedding(
                source_type=source.source_type,
                source_id=source.source_id,
                chunk_key=source.chunk_key,
                title=source.title,
                content=source.content,
                content_hash=_hash_text(source.content),
            )
            db.add(record)
        return record

    @staticmethod
    def _source_for_wiki(page: WikiPage) -> EmbeddingSource:
        content = "\n\n".join(part for part in [page.title, page.summary, page.body] if part)
        return EmbeddingSource(
            source_type="wiki",
            source_id=page.page_key,
            chunk_key=f"wiki:{page.page_key}:page",
            title=page.title,
            content=content,
            metadata={"path": page.path, "page_type": page.page_type, "tags": page.tags or []},
        )

    @staticmethod
    def _source_for_memory(memory: Memory) -> EmbeddingSource:
        return EmbeddingSource(
            source_type="memory",
            source_id=str(memory.id),
            chunk_key=f"memory:{memory.id}",
            title=memory.kind,
            content=memory.content,
            metadata={"kind": memory.kind, "source": memory.source, "importance": memory.importance, "confidence": memory.confidence},
        )

    @staticmethod
    def _source_for_skill_file(file: SkillFile) -> EmbeddingSource:
        return EmbeddingSource(
            source_type="skill",
            source_id=f"{file.skill_key}:{file.file_path}",
            chunk_key=f"skill:{file.skill_key}:{file.file_path}",
            title=f"{file.skill_key}/{file.file_path}",
            content=file.content,
            metadata={"skill_key": file.skill_key, "file_path": file.file_path, "checksum": file.checksum},
        )

    def _postgres_vector_search(self, db: Session, *, source_type: str, vector: list[float], limit: int) -> list[dict[str, Any]]:
        rows = db.execute(
            text(
                """
                SELECT id, source_type, source_id, chunk_key, title, content, metadata, dimensions,
                       1 - (vector_embedding <=> CAST(:vector AS vector)) AS vector_score
                FROM knowledge_embeddings
                WHERE source_type = :source_type
                  AND vector_status = 'embedded'
                  AND stale = false
                  AND vector_embedding IS NOT NULL
                ORDER BY vector_embedding <=> CAST(:vector AS vector)
                LIMIT :limit
                """
            ),
            {"source_type": source_type, "vector": _vector_literal(vector), "limit": limit},
        ).mappings()
        return [
            {
                "id": str(row["id"]),
                "source_type": row["source_type"],
                "source_id": row["source_id"],
                "chunk_key": row["chunk_key"],
                "title": row["title"],
                "content": row["content"],
                "metadata": row["metadata"] or {},
                "vector_score": round(float(row["vector_score"] or 0.0), 6),
                "hybrid_score": round(float(row["vector_score"] or 0.0), 6),
            }
            for row in rows
        ]

    def _python_vector_search(self, db: Session, *, source_type: str, vector: list[float], limit: int) -> list[dict[str, Any]]:
        records = list(
            db.scalars(
                select(KnowledgeEmbedding)
                .where(
                    KnowledgeEmbedding.source_type == source_type,
                    KnowledgeEmbedding.vector_status == "embedded",
                    KnowledgeEmbedding.stale.is_(False),
                )
                .limit(1000)
            ).all()
        )
        scored: list[dict[str, Any]] = []
        for record in records:
            score = _cosine_similarity(vector, [float(item) for item in (record.embedding or [])])
            scored.append(
                {
                    "id": str(record.id),
                    "source_type": record.source_type,
                    "source_id": record.source_id,
                    "chunk_key": record.chunk_key,
                    "title": record.title,
                    "content": record.content,
                    "metadata": record.metadata_json or {},
                    "vector_score": round(score, 6),
                    "hybrid_score": round(score, 6),
                }
            )
        scored.sort(key=lambda item: item["vector_score"], reverse=True)
        return scored[:limit]

    @staticmethod
    def _db_backend(db: Session) -> str:
        bind = db.get_bind()
        return bind.dialect.name if bind is not None else "unknown"


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _vector_literal(vector: list[float]) -> str:
    return "[" + ",".join(f"{float(value):.12g}" for value in vector) + "]"


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    if not left or not right or len(left) != len(right):
        return 0.0
    dot = sum(a * b for a, b in zip(left, right, strict=True))
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if left_norm <= 0 or right_norm <= 0:
        return 0.0
    return dot / (left_norm * right_norm)
