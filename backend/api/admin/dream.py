"""Dream/self-evolution review API."""

from __future__ import annotations

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_dream_runtime
from backend.runtime.dream import DreamRuntime

router = APIRouter(prefix="/api/dream", tags=["dream"], dependencies=[Depends(get_current_user)])


class DreamRunRequest(BaseModel):
    window_hours: int = 24
    limit: int = 50


@router.post("/run")
def run_dream(
    payload: DreamRunRequest,
    db: Session = Depends(get_db),
    runtime: DreamRuntime = Depends(get_dream_runtime),
) -> dict:
    result = runtime.run_review(db, window_hours=payload.window_hours, limit=payload.limit)
    return {
        "scanned_memories": result.scanned_memories,
        "failed_tool_runs": result.failed_tool_runs,
        "proposals_created": result.proposals_created,
        "proposal_ids": result.proposal_ids,
    }
