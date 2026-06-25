"""Skill repository and evolution proposal API."""

from __future__ import annotations

import uuid
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_skill_service
from backend.api.admin.serializers import proposal_to_dict, skill_file_to_dict, skill_to_dict
from backend.domain.skills import SkillService
from backend.infra.models import User

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(get_current_user)])


class ProposalCreateRequest(BaseModel):
    target_type: str = "skill"
    action: str = "upsert"
    risk_level: str = "medium"
    payload: dict[str, Any]
    evidence: dict[str, Any] = Field(default_factory=dict)


@router.get("")
def list_skills(
    status: str | None = None,
    limit: int = 200,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    return {"items": [skill_to_dict(item) for item in service.list(db, status=status, limit=limit)]}


@router.post("/scan")
def scan_skills(
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    return service.scan(db)


@router.get("/proposals")
def list_proposals(
    status: str | None = None,
    limit: int = 100,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    return {"items": [proposal_to_dict(item) for item in service.list_proposals(db, status=status, limit=limit)]}


@router.post("/proposals")
def create_proposal(
    payload: ProposalCreateRequest,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    proposal = service.create_proposal(
        db,
        target_type=payload.target_type,
        action=payload.action,
        payload=payload.payload,
        evidence=payload.evidence,
        risk_level=payload.risk_level,
    )
    return proposal_to_dict(proposal)


@router.post("/proposals/{proposal_id}/apply")
def apply_proposal(
    proposal_id: uuid.UUID,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        proposal = service.apply_proposal(db, proposal_id, actor=user.username)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return proposal_to_dict(proposal)


@router.post("/{skill_key:path}/test")
def test_skill(
    skill_key: str,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    return service.test(db, skill_key)


@router.post("/{skill_key:path}/rollback")
def rollback_skill(
    skill_key: str,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
    user: User = Depends(get_current_user),
) -> dict:
    try:
        return service.rollback(db, skill_key, actor=user.username)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc


@router.get("/{skill_key:path}")
def get_skill(
    skill_key: str,
    db: Session = Depends(get_db),
    service: SkillService = Depends(get_skill_service),
) -> dict:
    skill = service.get(db, skill_key)
    if skill is None:
        raise HTTPException(status_code=404, detail="skill not found")
    data = skill_to_dict(skill)
    data["files"] = [skill_file_to_dict(item) for item in service.files(db, skill_key)]
    return data
