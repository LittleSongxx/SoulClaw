"""Request and trace context helpers."""

from __future__ import annotations

import re
import uuid
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Iterator


_TRACE_ID: ContextVar[str] = ContextVar("soulclaw_trace_id", default="")
_REQUEST_ID: ContextVar[str] = ContextVar("soulclaw_request_id", default="")


@dataclass(frozen=True)
class TraceContext:
    trace_id: str
    request_id: str


def current_trace_id() -> str:
    return _TRACE_ID.get()


def current_request_id() -> str:
    return _REQUEST_ID.get()


def current_trace_context() -> TraceContext:
    return TraceContext(trace_id=current_trace_id(), request_id=current_request_id())


@contextmanager
def bind_trace_context(trace_id: str = "", request_id: str = "") -> Iterator[TraceContext]:
    context = normalize_trace_context(trace_id=trace_id, request_id=request_id)
    trace_token = _TRACE_ID.set(context.trace_id)
    request_token = _REQUEST_ID.set(context.request_id)
    try:
        yield context
    finally:
        _TRACE_ID.reset(trace_token)
        _REQUEST_ID.reset(request_token)


def normalize_trace_context(*, trace_id: str = "", request_id: str = "") -> TraceContext:
    trace_id = _clean_token(trace_id) or _clean_token(request_id) or uuid.uuid4().hex
    request_id = _clean_token(request_id) or trace_id
    return TraceContext(trace_id=trace_id, request_id=request_id)


def trace_id_from_traceparent(value: str) -> str:
    parts = value.strip().split("-")
    if len(parts) >= 4 and re.fullmatch(r"[0-9a-fA-F]{32}", parts[1] or ""):
        return parts[1].lower()
    return ""


def traceparent_from_trace_id(trace_id: str) -> str:
    trace_id = _clean_token(trace_id) or uuid.uuid4().hex
    return f"00-{trace_id[:32].ljust(32, '0')}-{uuid.uuid4().hex[:16]}-01"


def _clean_token(value: str) -> str:
    token = str(value or "").strip()
    if not token:
        return ""
    token = re.sub(r"[^a-zA-Z0-9_.:-]", "", token)
    return token[:128]
