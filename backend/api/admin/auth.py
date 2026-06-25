"""Local admin JWT authentication."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_event_bus
from backend.infra.config import Settings, get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import User
from backend.infra.security import create_access_token, verify_password

router = APIRouter(prefix="/api/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str
    password: str


@router.post("/login")
def login(
    payload: LoginRequest,
    db: Session = Depends(get_db),
    settings: Settings = Depends(get_settings),
    events: RuntimeEventBus = Depends(get_event_bus),
) -> dict:
    user = db.scalar(select(User).where(User.username == payload.username, User.is_active.is_(True)))
    if user is None or not verify_password(payload.password, user.password_hash):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="invalid credentials")
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

