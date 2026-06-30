"""FastAPI dependencies for authenticated admin APIs."""

from __future__ import annotations

from collections.abc import AsyncGenerator

import jwt
from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.infra.config import Settings, get_settings
from backend.infra.db import new_session
from backend.infra.models import User
from backend.infra.security import decode_access_token, parse_user_id

bearer = HTTPBearer(auto_error=False)


async def get_db(request: Request) -> AsyncGenerator[Session, None]:
    db = new_session()
    event_bus = getattr(request.app.state, "event_bus", None)
    session_binding = event_bus.bind_session(db) if event_bus is not None and hasattr(event_bus, "bind_session") else None
    try:
        if session_binding is not None:
            session_binding.__enter__()
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        if session_binding is not None:
            session_binding.__exit__(None, None, None)
        db.close()


def get_current_user(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer),
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
) -> User:
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="missing bearer token")
    try:
        payload = decode_access_token(credentials.credentials, settings)
    except jwt.PyJWTError as exc:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid bearer token") from exc
    user_id = parse_user_id(str(payload.get("sub") or ""))
    if user_id is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid token subject")
    user = db.scalar(select(User).where(User.id == user_id, User.is_active.is_(True)))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="user not found")
    return user


def get_wiki_service(request: Request):
    return request.app.state.wiki_service


def get_memory_service(request: Request):
    return request.app.state.memory_service


def get_skill_service(request: Request):
    return request.app.state.skill_service


def get_conversation_service(request: Request):
    return request.app.state.conversation_service


def get_job_service(request: Request):
    return request.app.state.job_service


def get_evolution_service(request: Request):
    return request.app.state.evolution_service


def get_platform_service(request: Request):
    return request.app.state.platform_service


def get_tool_registry(request: Request):
    return request.app.state.tool_registry


def get_tool_executor(request: Request):
    return request.app.state.tool_executor


def get_event_bus(request: Request):
    return request.app.state.event_bus


def get_agent_runtime(request: Request):
    return request.app.state.agent_runtime


def get_llm_client(request: Request):
    return request.app.state.llm_client


def get_a2a_service(request: Request):
    return request.app.state.a2a_service


def get_a2a_runtime(request: Request):
    return request.app.state.a2a_runtime


def get_mcp_runtime(request: Request):
    return request.app.state.mcp_runtime


def get_gateway_runtime(request: Request):
    return request.app.state.gateway_runtime


def get_dream_runtime(request: Request):
    return request.app.state.dream_runtime


def get_workspace_service(request: Request):
    return request.app.state.workspace_service


def get_heartbeat_runtime(request: Request):
    return request.app.state.heartbeat_runtime
