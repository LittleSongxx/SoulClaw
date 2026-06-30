"""Memory management API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_memory_service
from backend.api.admin.serializers import conflict_to_dict, memory_history_to_dict, memory_to_dict, probe_to_dict
from backend.domain.memory import MemoryService

router = APIRouter(prefix="/api/memory", tags=["memory"], dependencies=[Depends(get_current_user)])


class MemoryCreateRequest(BaseModel):
    kind: str = "agent_note"
    content: str
    source: str = "explicit"
    pinned: bool = False
    importance: float = Field(default=0.5, ge=0.0, le=1.0)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    stability: float = Field(default=0.5, ge=0.0, le=1.0)
    supersedes_id: uuid.UUID | None = None
    source_turn_id: str = ""
    metadata: dict[str, Any] = Field(default_factory=dict)


class MemorySearchRequest(BaseModel):
    query: str = ""
    limit: int = 10


class MemorySupersedeRequest(BaseModel):
    content: str
    kind: str | None = None
    source: str = "supersede"
    pinned: bool | None = None
    importance: float | None = None
    confidence: float | None = None
    stability: float | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)


class ProbeCreateRequest(BaseModel):
    question: str
    expected: str = ""


class MemoryRestoreRequest(BaseModel):
    history_id: uuid.UUID


@router.get("")
def list_memory(
    kind: str | None = None,
    include_archived: bool = False,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return {"items": [memory_to_dict(item) for item in service.list(db, kind=kind, include_archived=include_archived, limit=limit)]}


@router.post("")
def create_memory(
    payload: MemoryCreateRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    memory = service.create(db, **payload.model_dump())
    return memory_to_dict(memory)


@router.post("/search")
def search_memory(
    payload: MemorySearchRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return {
        "items": [
            {
                "score": item["score"],
                "source": item["source"],
                "match_reasons": item.get("match_reasons", []),
                "snippet": item.get("snippet", ""),
                "id": str(item["memory"].id),
                "kind": item["memory"].kind,
                "summary": item["memory"].content[:500],
                "memory": memory_to_dict(item["memory"]),
            }
            for item in service.search(db, payload.query, limit=payload.limit)
        ]
    }


@router.get("/search/status")
def search_status(
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return service.fts_status(db)


@router.post("/search/refresh")
def refresh_search_index(
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return service.refresh_fts(db)


@router.get("/governance")
def governance(
    budget_chars: int = 12000,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return service.governance_status(db, budget_chars=budget_chars)


@router.post("/sync")
def sync_memory_file(
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return service.sync_from_memory_file(db)


@router.get("/get")
def get_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    memory = service.get(db, memory_id)
    if memory is None:
        raise HTTPException(status_code=404, detail="memory not found")
    return memory_to_dict(memory)


@router.get("/history")
def history(
    memory_id: uuid.UUID | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return {"items": [memory_history_to_dict(item) for item in service.history(db, memory_id=memory_id, limit=limit)]}


@router.post("/restore")
def restore(
    payload: MemoryRestoreRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    try:
        return memory_to_dict(service.restore(db, payload.history_id))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/{memory_id}/supersede")
def supersede_memory(
    memory_id: uuid.UUID,
    payload: MemorySupersedeRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    try:
        memory = service.supersede(db, memory_id, payload.model_dump(exclude_none=True))
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return memory_to_dict(memory)


@router.post("/{memory_id}/verify")
def verify_memory(
    memory_id: uuid.UUID,
    confidence_delta: float = 0.05,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    try:
        memory = service.mark_verified(db, memory_id, confidence_delta=confidence_delta)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return memory_to_dict(memory)


@router.post("/{memory_id}/archive")
def archive_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    try:
        memory = service.archive(db, memory_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return memory_to_dict(memory)


@router.get("/conflicts")
def conflicts(
    status: str | None = "open",
    limit: int = 100,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return {"items": [conflict_to_dict(item) for item in service.list_conflicts(db, status=status, limit=limit)]}


@router.get("/probes")
def probes(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return {"items": [probe_to_dict(item) for item in service.list_probes(db, status=status, limit=limit)]}


@router.post("/probes")
def create_probe(
    payload: ProbeCreateRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
) -> dict:
    return probe_to_dict(service.create_probe(db, question=payload.question, expected=payload.expected))
