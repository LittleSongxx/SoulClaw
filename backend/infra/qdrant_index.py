"""Qdrant/FastEmbed hybrid index adapter."""

from __future__ import annotations

import os
import uuid
from dataclasses import dataclass
from typing import Any

from loguru import logger

from .config import Settings, get_settings


@dataclass(frozen=True)
class IndexDocument:
    key: str
    text: str
    payload: dict[str, Any]


class QdrantHybridIndex:
    """Thin adapter around qdrant-client's FastEmbed integration.

    Qdrant is the authoritative vector mirror, not the source of record. All
    methods fail soft and emit warnings so Postgres-backed management flows keep
    working when the vector service or first model download is unavailable.
    """

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: Any | None = None
        self._models_configured = False
        os.environ.setdefault("FASTEMBED_CACHE_PATH", str(self.settings.fastembed_cache_dir))
        os.environ.setdefault("HF_HOME", str(self.settings.fastembed_cache_dir))

    @property
    def enabled(self) -> bool:
        return self.settings.qdrant_enabled

    def _get_client(self) -> Any:
        if not self.enabled:
            raise RuntimeError("Qdrant is disabled")
        if self._client is None:
            from qdrant_client import QdrantClient

            kwargs: dict[str, Any] = {
                "url": self.settings.qdrant_url,
                "check_compatibility": False,
            }
            if self.settings.qdrant_api_key:
                kwargs["api_key"] = self.settings.qdrant_api_key
            client = QdrantClient(**kwargs)
            self._client = client
        return self._client

    def _ensure_models(self) -> Any:
        client = self._get_client()
        if not self._models_configured:
            client.set_model(self.settings.qdrant_dense_model)
            client.set_sparse_model(self.settings.qdrant_sparse_model)
            self._models_configured = True
        return client

    def ensure_collections(self) -> None:
        logger.info("[qdrant] startup collection checks deferred; vector mirrors initialize lazily")

    def upsert_documents(self, collection: str, documents: list[IndexDocument]) -> bool:
        if not documents:
            return True
        try:
            client = self._ensure_models()
            client.add(
                collection_name=collection,
                documents=[item.text for item in documents],
                metadata=[item.payload | {"document_key": item.key} for item in documents],
                ids=[self._stable_id(collection, item.key) for item in documents],
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qdrant] upsert failed for {} docs into {}: {}", len(documents), collection, exc)
            return False

    def search(self, collection: str, query: str, limit: int = 8) -> list[dict[str, Any]]:
        if not query.strip():
            return []
        try:
            client = self._ensure_models()
            results = client.query(collection_name=collection, query_text=query, limit=limit)
            items: list[dict[str, Any]] = []
            for result in results:
                metadata = getattr(result, "metadata", None) or getattr(result, "payload", None) or {}
                score = getattr(result, "score", None)
                items.append({"score": score, "payload": metadata})
            return items
        except Exception as exc:  # noqa: BLE001
            logger.warning("[qdrant] search failed in {}: {}", collection, exc)
            return []

    @staticmethod
    def _stable_id(collection: str, key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"zlagent:{collection}:{key}"))
