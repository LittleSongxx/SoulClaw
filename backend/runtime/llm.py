"""OpenAI-compatible chat client for the ZLAgent runtime."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx

from backend.infra.config import Settings, get_settings


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


class OpenAICompatibleClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()

    @property
    def configured(self) -> bool:
        return bool(self.settings.openai_api_key and self.settings.openai_model)

    def complete(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if not self.configured:
            raise RuntimeError("LLM is not configured")
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
        with httpx.Client(timeout=60) as client:
            response = client.post(url, headers=headers, json=payload)
            response.raise_for_status()
            raw = response.json()
        message = raw["choices"][0]["message"]
        tool_calls = self._parse_tool_calls(message.get("tool_calls") or [])
        return LLMResponse(content=message.get("content") or "", tool_calls=tool_calls, raw=raw)

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
