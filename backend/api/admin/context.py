"""Core context block APIs."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_core_context_service,
    get_current_user,
    get_db,
    get_evolution_proposal_service,
)
from backend.api.admin.serializers import core_context_block_to_dict, proposal_to_dict
from backend.domain.core_context import CoreContextService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.infra.models import User

router = APIRouter(prefix="/api/context", tags=["context"], dependencies=[Depends(get_current_user)])


class CoreContextProposalRequest(BaseModel):
    content: str
    title: str | None = None
    action: str = "update"
    risk_level: str = "medium"
    evidence: dict[str, Any] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


@router.get("/blocks")
def context_blocks(
    db: Session = Depends(get_db),
    service: CoreContextService = Depends(get_core_context_service),
) -> dict:
    return {"items": [core_context_block_to_dict(item) for item in service.list(db)]}


@router.get("/blocks/{block_key}")
def context_block(
    block_key: str,
    db: Session = Depends(get_db),
    service: CoreContextService = Depends(get_core_context_service),
) -> dict:
    service.ensure_defaults(db)
    block = service.get(db, block_key)
    if block is None:
        raise HTTPException(status_code=404, detail="core context block not found")
    return core_context_block_to_dict(block)


@router.post("/blocks/{block_key}/proposals")
def propose_context_block(
    block_key: str,
    payload: CoreContextProposalRequest,
    db: Session = Depends(get_db),
    service: CoreContextService = Depends(get_core_context_service),
    proposals: EvolutionProposalService = Depends(get_evolution_proposal_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        normalized = block_key.strip().lower()
        current = service.get(db, normalized)
        proposal = proposals.create(
            db,
            target_type="core_context",
            action=payload.action,
            payload={
                "block_key": normalized,
                "title": payload.title if payload.title is not None else (current.title if current is not None else None),
                "content": payload.content,
                "metadata": payload.metadata,
                "source": "api",
            },
            risk_level=payload.risk_level,
            evidence={**payload.evidence, "actor": user.username},
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


@router.get("/projections")
def context_projections(
    db: Session = Depends(get_db),
    service: CoreContextService = Depends(get_core_context_service),
) -> dict:
    return {"items": [projection.__dict__ for projection in service.projections(db)]}
