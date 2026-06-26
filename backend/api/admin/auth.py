"""Local admin JWT authentication."""

from __future__ import annotations

import time

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_event_bus
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import User
from backend.infra.security import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])
_LOGIN_FAILURES: dict[str, tuple[int, float]] = {}


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(
    payload: LoginRequest,
    request: Request,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    events: RuntimeEventBus = Depends(get_event_bus),
) -> dict:
    _check_login_rate_limit(request, settings)
    user = db.scalar(select(User).where(User.username == payload.username, User.is_active.is_(True)))
    if user is None or not verify_password(payload.password, user.password_hash):
        _record_login_failure(request, settings)
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
    _clear_login_failures(request, settings)
    token = create_access_token(user, settings)
    events.audit("auth.login", "user", actor_id=user.id, target_id=str(user.id))
    return {"access_token": token, "token_type": "bearer", "user": {"username": user.username, "role": user.role}}


@router.get("/me")
def me(user: User = Depends(get_current_user)) -> dict:
    return {"id": str(user.id), "username": user.username, "role": user.role, "is_active": user.is_active}


@router.post("/logout")
def logout(user: User = Depends(get_current_user), events: RuntimeEventBus = Depends(get_event_bus)) -> dict:
    events.audit("auth.logout", "user", actor_id=user.id, target_id=str(user.id))
    return {"ok": True}


def _client_key(request: Request) -> str:
    host = request.client.host if request.client else "unknown"
    return f"login:{host}"


def _check_login_rate_limit(request: Request, settings: Settings) -> None:
    if not settings.login_rate_limit_enabled:
        return
    key = _client_key(request)
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is not None:
        count = int(redis_client.get(key) or 0)
        if count >= 8:
            raise HTTPException(status_code=429, detail="too many login attempts")
        return
    count, until = _LOGIN_FAILURES.get(key, (0, 0.0))
    if count >= 8 and until > time.time():
        raise HTTPException(status_code=429, detail="too many login attempts")


def _record_login_failure(request: Request, settings: Settings) -> None:
    if not settings.login_rate_limit_enabled:
        return
    key = _client_key(request)
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is not None:
        count = redis_client.incr(key)
        if int(count) == 1:
            redis_client.expire(key, 900)
        return
    count, _until = _LOGIN_FAILURES.get(key, (0, 0.0))
    _LOGIN_FAILURES[key] = (count + 1, time.time() + 900)


def _clear_login_failures(request: Request, settings: Settings) -> None:
    if not settings.login_rate_limit_enabled:
        return
    key = _client_key(request)
    redis_client = getattr(request.app.state, "redis_client", None)
    if redis_client is not None:
        redis_client.delete(key)
        return
    _LOGIN_FAILURES.pop(key, None)
