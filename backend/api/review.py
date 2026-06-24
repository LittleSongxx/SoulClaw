"""REST surface for the v0.43 Phase B daily end-of-day review.

Mirrors :mod:`backend.api.curator`:

* ``GET  /api/review/state`` — last-run summary + service health.
* ``POST /api/review/run``   — fire a review pass right now (blocks
  until the LLM finishes; intended for operator debugging or a manual
  end-of-day kick when the cron-based 24h cadence isn't enough).
* ``GET  /api/review/log`` — most recent audit rows (one per run).
* ``POST /api/review/log/{run_id}/rollback`` — reverse memory effects
  of run ``run_id``; skill file changes are NOT auto-reverted (see
  :meth:`DailyReviewService.rollback_run` docstring for rationale).

v0.43 Phase B+ split the "run" and "log" concerns: ``state`` is
last-run convenience, ``log`` is the append-only audit trail. The
logs give the operator an emergency brake when the review fork
decides something they don't like.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

from ..evolution import EvolutionProposalError, EvolutionProposalStore
from ..evolution.store import STATUS_APPROVED, STATUS_APPLIED

router = APIRouter(prefix="/api/review", tags=["review"])


class RollbackBody(BaseModel):
    """Optional payload for POST /log/{run_id}/rollback."""

    note: Optional[str] = None


class ProposalCreateBody(BaseModel):
    target_type: str = Field(description="memory | skill | wiki | workflow")
    action: str
    payload: dict[str, Any]
    evidence: dict[str, Any] = Field(default_factory=dict)
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    risk_level: str = "medium"
    source: str = "operator"


class ProposalRejectBody(BaseModel):
    reason: Optional[str] = None


class ProposalApplyBody(BaseModel):
    auto_approve: bool = False


def _proposal_store(request: Request) -> EvolutionProposalStore:
    store = getattr(request.app.state, "proposal_store", None)
    if store is None:
        store = EvolutionProposalStore()
        request.app.state.proposal_store = store
    return store


@router.get("/state")
def get_state(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="daily review service unavailable",
        )
    return {
        "last_run_at": svc.last_run_at,
        "last_summary": svc.last_summary,
        "service_enabled": svc.enabled,
        "push_target_id": svc.push_target_id,
        "last_push_status": svc.last_push_status,
        "proposal_stats": _proposal_store(request).stats(),
    }


@router.get("/proposals")
def list_proposals(
    request: Request,
    status: Optional[str] = Query(None),
    target_type: Optional[str] = Query(None),
    limit: int = Query(default=100, ge=1, le=500),
) -> dict[str, Any]:
    try:
        proposals = _proposal_store(request).list(
            status=status,
            target_type=target_type,
            limit=limit,
        )
    except EvolutionProposalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "count": len(proposals), "proposals": proposals}


@router.post("/proposals", status_code=201)
def create_proposal(payload: ProposalCreateBody, request: Request) -> dict[str, Any]:
    try:
        proposal = _proposal_store(request).create(
            target_type=payload.target_type,
            action=payload.action,
            payload=payload.payload,
            evidence=payload.evidence,
            confidence=payload.confidence,
            risk_level=payload.risk_level,
            source=payload.source,
        )
    except EvolutionProposalError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return {"ok": True, "proposal": proposal}


@router.get("/proposals/{proposal_id}")
def get_proposal(proposal_id: int, request: Request) -> dict[str, Any]:
    proposal = _proposal_store(request).get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal #{proposal_id} not found")
    return {"ok": True, "proposal": proposal}


@router.post("/proposals/{proposal_id}/approve")
def approve_proposal(proposal_id: int, request: Request) -> dict[str, Any]:
    try:
        proposal = _proposal_store(request).approve(proposal_id)
    except EvolutionProposalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "proposal": proposal}


@router.post("/proposals/{proposal_id}/reject")
def reject_proposal(
    proposal_id: int,
    request: Request,
    body: Optional[ProposalRejectBody] = None,
) -> dict[str, Any]:
    try:
        proposal = _proposal_store(request).reject(
            proposal_id,
            reason=body.reason if body is not None and body.reason else "",
        )
    except EvolutionProposalError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return {"ok": True, "proposal": proposal}


@router.post("/proposals/{proposal_id}/apply")
async def apply_proposal(
    proposal_id: int,
    request: Request,
    body: Optional[ProposalApplyBody] = None,
) -> dict[str, Any]:
    store = _proposal_store(request)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal #{proposal_id} not found")
    if proposal["status"] == STATUS_APPLIED:
        return {"ok": True, "proposal": proposal, "already_applied": True}
    auto_approve = bool(body.auto_approve) if body is not None else False
    if proposal["status"] != STATUS_APPROVED:
        if auto_approve and proposal["status"] == "pending":
            proposal = store.approve(proposal_id)
        else:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"proposal #{proposal_id} status is {proposal['status']!r}; "
                    "approve it before applying or pass auto_approve=true"
                ),
            )
    try:
        before, after, result = await _apply_proposal_payload(request, proposal)
        applied = store.mark_applied(proposal_id, before=before, after=after, result=result)
    except Exception as exc:  # noqa: BLE001
        failed = store.mark_failed(proposal_id, error=f"{type(exc).__name__}: {exc}")
        raise HTTPException(status_code=500, detail=failed["error"]) from exc
    return {"ok": True, "proposal": applied}


@router.post("/proposals/{proposal_id}/rollback")
def rollback_proposal(proposal_id: int, request: Request) -> dict[str, Any]:
    store = _proposal_store(request)
    proposal = store.get(proposal_id)
    if proposal is None:
        raise HTTPException(status_code=404, detail=f"proposal #{proposal_id} not found")
    if proposal["status"] != STATUS_APPLIED:
        raise HTTPException(
            status_code=409,
            detail=f"proposal #{proposal_id} status is {proposal['status']!r}; only applied proposals can roll back",
        )
    try:
        result = _rollback_proposal_payload(request, proposal)
        rolled = store.mark_rolled_back(proposal_id, result=result)
    except EvolutionProposalError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {"ok": True, "proposal": rolled}


@router.post("/run")
async def run_now(request: Request) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None:
        raise HTTPException(
            status_code=503, detail="daily review service unavailable",
        )
    summary = await svc.run_once()
    if summary is None:
        # The service swallows internal errors and logs them. From the
        # operator's perspective a 500 with a clear pointer at the
        # logs is the most useful failure mode here.
        raise HTTPException(
            status_code=500,
            detail="daily review failed (see logs); service continues running",
        )
    return {
        "ok": True,
        "last_run_at": svc.last_run_at,
        "summary": summary,
    }


@router.get("/log")
def get_log(
    request: Request,
    limit: int = Query(default=50, ge=1, le=500),
) -> dict[str, Any]:
    """Return recent review runs, newest first, each with rollback state."""
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    runs = svc.action_log.list_runs(limit=limit)
    return {"ok": True, "count": len(runs), "runs": runs}


@router.get("/log/{run_id}")
def get_log_entry(request: Request, run_id: int) -> dict[str, Any]:
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    row = svc.action_log.get_run(run_id)
    if row is None:
        raise HTTPException(
            status_code=404, detail=f"review run #{run_id} not found",
        )
    return {"ok": True, "run": row}


@router.post("/log/{run_id}/rollback")
def rollback_run(
    request: Request,
    run_id: int,
    body: Optional[RollbackBody] = None,
) -> dict[str, Any]:
    """Archive new memory rows + unarchive previously-archived ones.

    Idempotent: calling twice returns ``ok=False, reason="already
    rolled back"`` on the second attempt. Skill file changes are
    NOT auto-reverted — the JSON response surfaces the affected
    skill_history slice so the operator can ``git revert`` or use
    the existing ``/api/skills/{name}`` endpoints manually.
    """
    svc = getattr(request.app.state, "daily_review_service", None)
    if svc is None or svc.action_log is None:
        raise HTTPException(
            status_code=503, detail="review audit log unavailable",
        )
    note = body.note if body is not None else None
    result = svc.rollback_run(int(run_id), note=note)
    # 200 for both ok and idempotent-no-op; 404 only when the run
    # doesn't exist. Lets the UI distinguish via payload.ok.
    if not result.get("ok") and str(result.get("reason", "")).startswith(
        "run #"
    ) and "not found" in str(result.get("reason", "")):
        raise HTTPException(status_code=404, detail=result["reason"])
    return result


async def _apply_proposal_payload(
    request: Request,
    proposal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    target = proposal["target_type"]
    action = proposal["action"]
    payload = proposal.get("payload") or {}
    if target == "memory":
        return _apply_memory_proposal(request, action, payload)
    if target == "wiki":
        return _apply_wiki_proposal(request, action, payload)
    if target == "workflow":
        return _apply_workflow_proposal(request, proposal)
    if target == "skill":
        return await _apply_skill_proposal(request, payload)
    raise EvolutionProposalError(f"unsupported proposal target_type {target!r}")


def _apply_memory_proposal(
    request: Request,
    action: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    store = getattr(request.app.state, "memory_store", None)
    if store is None:
        raise EvolutionProposalError("memory store unavailable")
    before: dict[str, Any] = {}
    after: dict[str, Any] = {}
    action = action.lower()
    if action in {"remember", "add"}:
        row = store.add(
            str(payload.get("content") or ""),
            kind=str(payload.get("kind") or "agent_note"),
            source=str(payload.get("source") or "review"),
            pinned=bool(payload.get("pinned") or False),
            knowledge_base_id=str(payload.get("knowledge_base_id") or "default"),
            importance=_payload_float(payload.get("importance"), 0.5),
            confidence=_payload_float(payload.get("confidence"), 0.5),
            stability=_payload_float(payload.get("stability"), 0.5),
            source_turn_id=_payload_optional_str(payload.get("source_turn_id")),
            supersedes=_payload_optional_int(payload.get("supersedes")),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
        )
        after = {"memory": row}
        return before, after, {"memory_id": row["id"], "action": "remember"}
    if action == "consolidate":
        kind_raw = payload.get("kind")
        kind = str(kind_raw).strip() if kind_raw not in (None, "") else None
        threshold = max(0.5, min(1.0, _payload_float(payload.get("similarity_threshold"), 0.80)))
        max_pairs = max(1, min(500, _payload_optional_int(payload.get("max_pairs")) or 50))
        before_candidates = store.list(kind=kind, include_archived=True, limit=2000)
        report = store.consolidate(
            kind=kind,
            similarity_threshold=threshold,
            max_pairs=max_pairs,
        )
        affected_ids = _consolidation_memory_ids(report)
        before_rows = [
            row for row in before_candidates
            if int(row.get("id") or 0) in affected_ids
        ]
        after_rows = [
            row for row in (store.get(memory_id) for memory_id in sorted(affected_ids))
            if row is not None
        ]
        before = {
            "candidate_count": len(before_candidates),
            "memories": before_rows,
        }
        after = {"memories": after_rows, "report": report}
        return before, after, {
            "action": "consolidate",
            "pairs_considered": report.get("pairs_considered", 0),
            "pairs_merged": report.get("pairs_merged", 0),
            "details": report.get("details") or [],
        }
    memory_id = int(payload.get("memory_id") or 0)
    if not memory_id:
        raise EvolutionProposalError("memory_id is required")
    before = {"memory": store.get(memory_id)}
    if action == "archive":
        ok = store.archive(memory_id)
    elif action == "unarchive":
        ok = store.unarchive(memory_id)
    elif action == "pin":
        ok = store.set_pinned(memory_id, True)
    elif action == "unpin":
        ok = store.set_pinned(memory_id, False)
    elif action in {"forget", "delete"}:
        ok = store.remove(memory_id, allow_pinned=bool(payload.get("allow_pinned") or False))
    else:
        raise EvolutionProposalError(f"unsupported memory action {action!r}")
    if not ok:
        raise EvolutionProposalError(f"memory #{memory_id} not found")
    after = {"memory": store.get(memory_id)}
    return before, after, {"memory_id": memory_id, "action": action}


def _payload_float(raw: Any, default: float) -> float:
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = default
    return max(0.0, min(1.0, value))


def _payload_optional_int(raw: Any) -> Optional[int]:
    if raw in (None, ""):
        return None
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _payload_optional_str(raw: Any) -> Optional[str]:
    if raw is None:
        return None
    value = str(raw).strip()
    return value or None


def _consolidation_memory_ids(report: dict[str, Any]) -> set[int]:
    ids: set[int] = set()
    for item in report.get("details") or []:
        for key in ("survivor_id", "archived_id"):
            try:
                value = int(item.get(key) or 0)
            except (TypeError, ValueError):
                value = 0
            if value > 0:
                ids.add(value)
    return ids


async def _apply_skill_proposal(
    request: Request,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    registry = getattr(request.app.state, "tool_registry", None)
    tool = registry.get("skill_manage") if registry is not None else None
    if tool is None or not hasattr(tool, "apply_approved"):
        raise EvolutionProposalError("skill_manage apply surface unavailable")
    before = _skill_snapshot(request, payload)
    result = await tool.apply_approved(dict(payload))
    if not result.ok:
        raise EvolutionProposalError(result.error or "skill_manage apply failed")
    after = _skill_snapshot(request, payload)
    return before, after, {
        "tool": "skill_manage",
        "ok": True,
        "content": result.content,
        "raw": result.raw or {},
    }


def _apply_wiki_proposal(
    request: Request,
    action: str,
    payload: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    wiki = getattr(request.app.state, "wiki_store", None)
    if wiki is None:
        raise EvolutionProposalError("wiki store unavailable")
    action = action.lower()
    before: dict[str, Any] = {}
    if action in {"add", "add_answer_cache", "add_atomic_fact"}:
        crystal_kind = str(payload.get("crystal_kind") or ("atomic_fact" if action == "add_atomic_fact" else "answer"))
        entry_id = wiki.add(
            str(payload.get("skill_id") or ""),
            str(payload.get("raw_query") or payload.get("query") or ""),
            str(payload.get("answer") or payload.get("claim") or ""),
            ttl_seconds=payload.get("ttl_seconds"),
            metadata=payload.get("metadata") if isinstance(payload.get("metadata"), dict) else None,
            confidence=payload.get("confidence"),
            sources=payload.get("sources") if isinstance(payload.get("sources"), list) else None,
            aliases=payload.get("aliases") if isinstance(payload.get("aliases"), list) else None,
            crystal_kind=crystal_kind,
            geo_path=str(payload.get("geo_path") or ""),
        )
        after = {"wiki_entry_id": entry_id}
        return before, after, {"wiki_entry_id": entry_id, "action": action}
    if action == "supersede":
        old_id = int(payload.get("old_id") or 0)
        new_id = int(payload.get("new_id") or 0)
        ok = wiki.supersede(old_id, new_id, reason=str(payload.get("reason") or "proposal applied"))
        if not ok:
            raise EvolutionProposalError("wiki supersede failed; old_id or new_id missing")
        return {"old_id": old_id, "new_id": new_id}, {"superseded": True}, {"action": action}
    if action in {"delete", "remove"}:
        entry_id = int(payload.get("entry_id") or payload.get("wiki_entry_id") or 0)
        ok = wiki.delete(entry_id)
        if not ok:
            raise EvolutionProposalError(f"wiki entry #{entry_id} not found")
        return {"wiki_entry_id": entry_id}, {"deleted": True}, {"wiki_entry_id": entry_id, "action": action}
    raise EvolutionProposalError(f"unsupported wiki action {action!r}")


def _apply_workflow_proposal(
    request: Request,
    proposal: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
    workspace = _workspace_dir(request)
    recipes_dir = workspace / "compose_recipes"
    recipes_dir.mkdir(parents=True, exist_ok=True)
    payload = proposal.get("payload") or {}
    name = _safe_slug(str(payload.get("name") or payload.get("title") or f"proposal-{proposal['id']}"))
    path = recipes_dir / f"{name}.json"
    before = _file_snapshot(path)
    body = {
        "name": name,
        "source_proposal_id": proposal["id"],
        "action": proposal["action"],
        "payload": payload,
        "evidence": proposal.get("evidence") or {},
    }
    path.write_text(json.dumps(body, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
    after = _file_snapshot(path)
    return before, after, {"recipe_path": str(path), "name": name}


def _rollback_proposal_payload(request: Request, proposal: dict[str, Any]) -> dict[str, Any]:
    target = proposal["target_type"]
    action = proposal["action"]
    result = proposal.get("result") or {}
    if target == "memory":
        memory_store = getattr(request.app.state, "memory_store", None)
        if memory_store is None:
            raise EvolutionProposalError("memory store unavailable")
        if action == "consolidate":
            before_rows = (proposal.get("before") or {}).get("memories") or []
            if before_rows:
                restored = _restore_memory_rows(before_rows)
                return {
                    "ok": True,
                    "rolled_back": "restored_consolidated_memories",
                    "restored_count": restored,
                }
            unarchived: list[int] = []
            for item in result.get("details") or []:
                archived_id = _payload_optional_int(item.get("archived_id"))
                if archived_id and memory_store.unarchive(archived_id):
                    unarchived.append(archived_id)
            return {
                "ok": bool(unarchived),
                "rolled_back": "unarchived_consolidated_memories",
                "memory_ids": unarchived,
            }
        memory_id = int(result.get("memory_id") or 0)
        if not memory_id:
            return {"ok": False, "reason": "no memory_id recorded"}
        if action in {"remember", "add"}:
            ok = memory_store.archive(memory_id)
            return {"ok": ok, "rolled_back": "archived_created_memory", "memory_id": memory_id}
        if action == "archive":
            ok = memory_store.unarchive(memory_id)
            return {"ok": ok, "rolled_back": "unarchived_memory", "memory_id": memory_id}
        return {"ok": False, "reason": f"no automatic rollback for memory action {action!r}"}
    if target == "wiki":
        wiki = getattr(request.app.state, "wiki_store", None)
        if wiki is None:
            raise EvolutionProposalError("wiki store unavailable")
        entry_id = int(result.get("wiki_entry_id") or 0)
        if action in {"add", "add_answer_cache", "add_atomic_fact"} and entry_id:
            return {"ok": wiki.delete(entry_id), "rolled_back": "deleted_created_wiki_entry", "wiki_entry_id": entry_id}
        return {"ok": False, "reason": f"no automatic rollback for wiki action {action!r}"}
    if target == "workflow":
        recipe_path = result.get("recipe_path")
        if not recipe_path:
            return {"ok": False, "reason": "no recipe_path recorded"}
        path = Path(str(recipe_path))
        workspace = _workspace_dir(request).resolve()
        try:
            path.resolve().relative_to(workspace)
        except ValueError as exc:
            raise EvolutionProposalError("recorded recipe path escapes workspace") from exc
        if path.exists():
            path.unlink()
        return {"ok": True, "rolled_back": "deleted_compose_recipe", "recipe_path": str(path)}
    return {"ok": False, "reason": f"manual rollback required for target_type {target!r}"}


def _workspace_dir(request: Request) -> Path:
    settings = getattr(request.app.state, "settings", None)
    if settings is not None and getattr(settings, "workspace_dir", None):
        return Path(settings.workspace_dir)
    return Path("/app/workspace")


def _restore_memory_rows(rows: list[dict[str, Any]]) -> int:
    """Best-effort restore of rows captured before a memory proposal apply."""
    from datetime import datetime

    from ..db.models import UserMemory
    from ..db.session import session_scope

    def _dt(value: Any):
        if not value:
            return None
        try:
            return datetime.fromisoformat(str(value))
        except ValueError:
            return None

    restored = 0
    with session_scope() as session:
        for snap in rows:
            try:
                memory_id = int(snap.get("id") or 0)
            except (TypeError, ValueError):
                continue
            if memory_id <= 0:
                continue
            row = session.get(UserMemory, memory_id)
            if row is None:
                continue
            row.archived = bool(snap.get("archived") or False)
            row.pinned = bool(snap.get("pinned") or False)
            row.recall_count = int(snap.get("recall_count") or 0)
            row.last_recalled_at = _dt(snap.get("last_recalled_at"))
            row.last_verified_at = _dt(snap.get("last_verified_at"))
            row.importance = _payload_float(snap.get("importance"), float(row.importance or 0.5))
            row.confidence = _payload_float(snap.get("confidence"), float(row.confidence or 0.5))
            row.stability = _payload_float(snap.get("stability"), float(row.stability or 0.5))
            row.supersedes = _payload_optional_int(snap.get("supersedes"))
            row.source_turn_id = _payload_optional_str(snap.get("source_turn_id"))
            if isinstance(snap.get("metadata"), dict):
                row.metadata_json = json.dumps(snap.get("metadata"), ensure_ascii=False, sort_keys=True)
            row.updated_at = _dt(snap.get("updated_at")) or row.updated_at
            restored += 1
    return restored


def _skill_snapshot(request: Request, payload: dict[str, Any]) -> dict[str, Any]:
    skill_name = str(payload.get("skill_name") or "").strip()
    file_path = str(payload.get("file_path") or "SKILL.md").strip() or "SKILL.md"
    if not skill_name:
        return {"skill_name": "", "exists": False}
    root = (_workspace_dir(request) / "skills").resolve()
    target = (root / skill_name / file_path).resolve()
    try:
        target.relative_to(root)
    except ValueError:
        return {"skill_name": skill_name, "file_path": file_path, "error": "path escapes skills root"}
    return _file_snapshot(target) | {"skill_name": skill_name, "file_path": file_path}


def _file_snapshot(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {"path": str(path), "exists": False, "content": None}
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        return {"path": str(path), "exists": True, "error": str(exc)}
    return {"path": str(path), "exists": True, "content": content}


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9._-]+", "-", value.strip().lower()).strip("-._")
    return slug[:80] or "compose-recipe"
