"""Dream/self-evolution review API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_job_service
from backend.api.admin.serializers import background_job_to_dict
from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.infra.models import User

router = APIRouter(prefix="/api/dream", tags=["dream"], dependencies=[Depends(get_current_user)])


class DreamRunRequest(BaseModel):
    window_hours: int = 24
    limit: int = 50


@router.post("/run")
def run_dream(
    payload: DreamRunRequest,
    db: Session = Depends(get_db),
    jobs: BackgroundJobService = Depends(get_job_service),
    user: User = Depends(get_current_user),
) -> dict:
    job = enqueue_background_job(
        db,
        task_name="dream_review",
        payload=payload.model_dump(),
        triggered_by=user.username,
        service=jobs,
    )
    return {"job": background_job_to_dict(job)}
