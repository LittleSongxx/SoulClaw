"""Tool registry and execution API."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_tool_executor, get_tool_registry
from backend.api.admin.serializers import tool_definition_to_dict
from backend.domain.tools import ToolExecutor, ToolRegistry

router = APIRouter(prefix="/api/tools", tags=["tools"], dependencies=[Depends(get_current_user)])


class ToolRunRequest(BaseModel):
    arguments: dict[str, Any] = Field(default_factory=dict)
    turn_id: str = ""
    approved: bool = False


@router.get("")
def list_tools(registry: ToolRegistry = Depends(get_tool_registry)) -> dict:
    return {"items": [tool_definition_to_dict(item) for item in registry.list()]}


@router.post("/{tool_name}/run")
def run_tool(
    tool_name: str,
    payload: ToolRunRequest,
    db: Session = Depends(get_db),
    executor: ToolExecutor = Depends(get_tool_executor),
) -> dict:
    try:
        return executor.execute(
            db,
            tool_name=tool_name,
            arguments=payload.arguments,
            turn_id=payload.turn_id,
            approved=payload.approved,
        )
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

