"""Skill repository API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_current_user,
    get_db,
    get_skill_service,
)
from backend.api.admin.serializers import skill_file_to_dict, skill_to_dict
from backend.domain.skills import SkillService
from backend.infra.models import User

router = APIRouter(prefix="/api/skills", tags=["skills"], dependencies=[Depends(get_current_user)])


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
