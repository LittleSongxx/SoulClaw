"""Memory management API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_core_context_service,
    get_current_user,
    get_db,
    get_evolution_proposal_service,
    get_memory_curator_service,
    get_memory_service,
)
from backend.api.admin.serializers import (
    conflict_to_dict,
    memory_history_to_dict,
    memory_to_dict,
    probe_to_dict,
    proposal_to_dict,
)
from backend.domain.core_context import CoreContextService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.domain.memory import MemoryService
from backend.domain.memory_curator import MemoryCuratorService
from backend.infra.models import User

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


class MemoryDraftImportRequest(BaseModel):
    kind: str = "memory"
    content: str


class MemoryCurateTurnRequest(BaseModel):
    session_id: str = "local"
    turn_id: str = ""
    user_message: str = ""
    assistant_answer: str = ""
    tool_results: list[dict[str, Any]] = Field(default_factory=list)
    a2a_result: dict[str, Any] = Field(default_factory=dict)


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
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    proposal = proposals.create(
        db,
        target_type="memory",
        action="create",
        payload=payload.model_dump(mode="json"),
        evidence={"source": "api.memory.create", "actor": user.username},
        risk_level="medium",
    )
    return proposal_to_dict(proposal)


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
                "hybrid_score": item.get("hybrid_score", item["score"]),
                "vector_score": item.get("vector_score", 0.0),
                "fts_score": item.get("fts_score", 0.0),
                "chunk_key": item.get("chunk_key", ""),
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
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        proposal = proposals.create(
            db,
            target_type="memory",
            action="restore",
            payload={
                "kind": "agent_note",
                "content": "Restore memory from history",
                "source": "api.memory.restore",
                "history_id": str(payload.history_id),
            },
            risk_level="medium",
            evidence={"source": "api.memory.restore", "actor": user.username, "history_id": str(payload.history_id)},
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


@router.post("/{memory_id}/supersede")
def supersede_memory(
    memory_id: uuid.UUID,
    payload: MemorySupersedeRequest,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    existing = service.get(db, memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    proposal = proposals.create(
        db,
        target_type="memory",
        action="supersede",
        payload={
            "memory_id": str(memory_id),
            "kind": payload.kind or existing.kind,
            "content": payload.content,
            "source": payload.source,
            "pinned": existing.pinned if payload.pinned is None else payload.pinned,
            "importance": existing.importance if payload.importance is None else payload.importance,
            "confidence": existing.confidence if payload.confidence is None else payload.confidence,
            "stability": existing.stability if payload.stability is None else payload.stability,
            "metadata": payload.metadata,
        },
        risk_level="medium",
        evidence={"source": "api.memory.supersede", "actor": user.username, "memory_id": str(memory_id)},
    )
    return proposal_to_dict(proposal)


@router.post("/{memory_id}/verify")
def verify_memory(
    memory_id: uuid.UUID,
    confidence_delta: float = 0.05,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    existing = service.get(db, memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    proposal = proposals.create(
        db,
        target_type="memory",
        action="verify",
        payload={
            "memory_id": str(memory_id),
            "kind": existing.kind,
            "content": existing.content,
            "source": "api.memory.verify",
            "pinned": existing.pinned,
            "importance": existing.importance,
            "confidence": existing.confidence,
            "stability": existing.stability,
            "confidence_delta": confidence_delta,
        },
        risk_level="low",
        evidence={"source": "api.memory.verify", "actor": user.username, "memory_id": str(memory_id)},
    )
    return proposal_to_dict(proposal)


@router.post("/{memory_id}/archive")
def archive_memory(
    memory_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: MemoryService = Depends(get_memory_service),
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    existing = service.get(db, memory_id)
    if existing is None:
        raise HTTPException(status_code=404, detail="memory not found")
    proposal = proposals.create(
        db,
        target_type="memory",
        action="archive",
        payload={
            "memory_id": str(memory_id),
            "kind": existing.kind,
            "content": existing.content,
            "source": "api.memory.archive",
            "pinned": existing.pinned,
            "importance": existing.importance,
            "confidence": existing.confidence,
            "stability": existing.stability,
        },
        risk_level="low",
        evidence={"source": "api.memory.archive", "actor": user.username, "memory_id": str(memory_id)},
    )
    return proposal_to_dict(proposal)


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


@router.post("/curate-turn")
def curate_turn(
    payload: MemoryCurateTurnRequest,
    db: Session = Depends(get_db),
    curator: MemoryCuratorService = Depends(get_memory_curator_service),
) -> dict:
    result = curator.curate_turn(
        db,
        session_id=payload.session_id,
        turn_id=payload.turn_id,
        user_message=payload.user_message,
        assistant_answer=payload.assistant_answer,
        tool_results=payload.tool_results,
        a2a_result=payload.a2a_result,
    )
    return {"items": [proposal_to_dict(item) for item in result.proposals], "created": result.created}


@router.post("/import-draft")
def import_draft(
    payload: MemoryDraftImportRequest,
    db: Session = Depends(get_db),
    core_context: CoreContextService = Depends(get_core_context_service),
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    normalized = payload.kind.strip().lower()
    evidence = {"source": "draft_import", "actor": user.username, "draft_kind": normalized}
    if normalized in {"soul", "user", "heartbeat"}:
        current = core_context.get(db, normalized)
        proposal = proposals.create(
            db,
            target_type="core_context",
            action="update",
            payload={
                "block_key": normalized,
                "title": current.title if current is not None else None,
                "content": payload.content,
                "metadata": {},
                "source": "draft_import",
            },
            evidence=evidence,
            risk_level="medium",
        )
    else:
        proposal = proposals.create(
            db,
            target_type="memory",
            action="create",
            payload={
                "kind": "agent_note",
                "content": payload.content,
                "source": "draft_import",
                "metadata": {"draft_kind": normalized, "actor": user.username},
            },
            evidence=evidence,
            risk_level="medium",
        )
    return proposal_to_dict(proposal)
