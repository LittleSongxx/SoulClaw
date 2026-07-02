"""OpenAI-compatible embedding client."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.rate_limit import FixedWindowRateLimiter
from backend.infra.resilience import ResilienceManager, ResiliencePolicy


@dataclass(frozen=True)
class EmbeddingResponse:
    vectors: list[list[float]]
    model: str
    dimensions: int
    raw: dict[str, Any]


class OpenAICompatibleEmbeddingClient:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        events: RuntimeEventBus | None = None,
        resilience: ResilienceManager | None = None,
        rate_limiter: FixedWindowRateLimiter | None = None,
    ) -> None:
        self.settings = settings or get_settings()
        self.events = events
        self.resilience = resilience or ResilienceManager(events=events)
        self.rate_limiter = rate_limiter
        self.policy = ResiliencePolicy(
            name="embedding.openai_compatible",
            max_attempts=3,
            base_delay_seconds=0.5,
            max_delay_seconds=8.0,
            failure_threshold=5,
            recovery_seconds=60.0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.openai_api_key and self.settings.embedding_model)

    def embed_texts(self, texts: list[str]) -> EmbeddingResponse:
        cleaned = [str(text or "").strip() for text in texts]
        if not cleaned or any(not text for text in cleaned):
            raise ValueError("embedding input must contain non-empty text")
        if self.rate_limiter is not None:
            self.rate_limiter.enforce(
                policy="embedding",
                identity=self.settings.embedding_model,
                limit=self.settings.rate_limit_llm_per_minute,
            )
        if not self.configured:
            raise RuntimeError("embedding provider is not configured")
        url = f"{self.settings.openai_base_url.rstrip('/')}/embeddings"
        payload: dict[str, Any] = {
            "model": self.settings.embedding_model,
            "input": cleaned,
        }
        if self.settings.embedding_pass_dimensions:
            payload["dimensions"] = self.settings.embedding_dimensions
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }

        def request() -> dict[str, Any]:
            with httpx.Client(timeout=60) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                data = response.json()
            return data if isinstance(data, dict) else {"data": data}

        raw = self.resilience.call(self.policy, request)
        items = raw.get("data") if isinstance(raw.get("data"), list) else []
        vectors: list[list[float]] = []
        for item in sorted([item for item in items if isinstance(item, dict)], key=lambda value: int(value.get("index") or 0)):
            vector = item.get("embedding")
            if not isinstance(vector, list):
                raise RuntimeError("embedding response item is missing embedding vector")
            try:
                values = [float(value) for value in vector]
            except (TypeError, ValueError) as exc:
                raise RuntimeError("embedding response vector contains non-numeric values") from exc
            if len(values) != self.settings.embedding_dimensions:
                raise RuntimeError(
                    f"embedding dimension mismatch: expected {self.settings.embedding_dimensions}, got {len(values)}"
                )
            vectors.append(values)
        if len(vectors) != len(cleaned):
            raise RuntimeError(f"embedding response count mismatch: expected {len(cleaned)}, got {len(vectors)}")
        return EmbeddingResponse(
            vectors=vectors,
            model=str(raw.get("model") or self.settings.embedding_model),
            dimensions=self.settings.embedding_dimensions,
            raw=raw,
        )

    def resilience_state(self) -> dict[str, Any]:
        return self.resilience.state()
