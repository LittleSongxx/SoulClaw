"""OpenAI-compatible chat client for the SoulClaw runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.rate_limit import FixedWindowRateLimiter
from backend.infra.resilience import ResilienceManager, ResiliencePolicy


@dataclass(frozen=True)
class LLMToolCall:
    name: str
    arguments: dict[str, Any]
    id: str = ""


@dataclass(frozen=True)
class LLMResponse:
    content: str
    tool_calls: list[LLMToolCall]
    raw: dict[str, Any]


@dataclass(frozen=True)
class LLMProvider:
    name: str
    model: str
    base_url: str
    context_window_tokens: int
    supports_tools: bool = True
    supports_streaming: bool = False


class LLMProviderRegistry:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    def list(self) -> list[LLMProvider]:
        return [
            LLMProvider(
                name=self.settings.llm_provider,
                model=self.settings.openai_model or "",
                base_url=self.settings.openai_base_url,
                context_window_tokens=self.settings.llm_context_window_tokens,
                supports_tools=True,
                supports_streaming=False,
            )
        ]

    def active(self) -> LLMProvider:
        return self.list()[0]


class OpenAICompatibleClient:
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
            name="llm.openai_compatible",
            max_attempts=3,
            base_delay_seconds=0.5,
            max_delay_seconds=8.0,
            failure_threshold=5,
            recovery_seconds=60.0,
        )

    @property
    def configured(self) -> bool:
        return bool(self.settings.openai_api_key and self.settings.openai_model)

    @property
    def provider(self) -> LLMProvider:
        return LLMProviderRegistry(self.settings).active()

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if not self.configured:
            raise RuntimeError("LLM is not configured")
        if self.rate_limiter is not None:
            self.rate_limiter.enforce(
                policy="llm",
                identity=self.settings.openai_model or self.settings.llm_provider,
                limit=self.settings.rate_limit_llm_per_minute,
            )
        url = f"{self.settings.openai_base_url.rstrip('/')}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.settings.openai_api_key}",
            "Content-Type": "application/json",
        }
        payload: dict[str, Any] = {
            "model": self.settings.openai_model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        def request() -> dict[str, Any]:
            with httpx.Client(timeout=60) as client:
                response = client.post(url, headers=headers, json=payload)
                response.raise_for_status()
                return response.json()

        raw = self.resilience.call(self.policy, request)
        message = raw["choices"][0]["message"]
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])
        return LLMResponse(content=message.get("content") or "", tool_calls=tool_calls, raw=raw)

    def resilience_state(self) -> dict[str, Any]:
        return self.resilience.state()

    @staticmethod
    def _parse_tool_calls(raw_calls: list[dict[str, Any]]) -> list[LLMToolCall]:
        calls: list[LLMToolCall] = []
        for call in raw_calls:
            function = call.get("function") or {}
            name = str(function.get("name") or "")
            if not name:
                continue
            raw_arguments = function.get("arguments") or "{}"
            try:
                arguments = json.loads(raw_arguments) if isinstance(raw_arguments, str) else dict(raw_arguments)
            except (TypeError, ValueError):
                arguments = {"_raw": raw_arguments}
            calls.append(LLMToolCall(name=name, arguments=arguments, id=str(call.get("id") or "")))
        return calls
