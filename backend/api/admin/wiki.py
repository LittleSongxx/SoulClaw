"""Wiki management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from backend.api.admin.deps import (
    get_current_user,
    get_db,
    get_job_service,
    get_llm_client,
    get_wiki_service,
)
from backend.api.admin.serializers import (
    background_job_to_dict,
    wiki_error_to_dict,
    wiki_page_to_dict,
)
from backend.domain.jobs import BackgroundJobService, enqueue_background_job
from backend.domain.wiki import WikiService
from backend.infra.models import User
from backend.runtime.llm import OpenAICompatibleClient

router = APIRouter(prefix="/api/wiki", tags=["wiki"], dependencies=[Depends(get_current_user)])


class WikiSearchRequest(BaseModel):
    query: str
    limit: int = 10
    strategy: str = ""
    path_prefix: str = ""
    include_constraints: bool = False


class WikiRouteRequest(BaseModel):
    query: str
    limit: int = 10


class WikiRepairRequest(BaseModel):
    enqueue: bool = True
    apply_safe: bool = True
    error_ids: list[str] = Field(default_factory=list)


class WikiLintRequest(BaseModel):
    enqueue: bool = True


class WikiCompileRequest(BaseModel):
    enqueue: bool = True


@router.get("/pages")
def pages(
    limit: int = 200,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return {"items": [wiki_page_to_dict(page) for page in service.list_pages(db, limit=limit)]}


@router.get("/read")
def read(
    page_key: str,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    page = service.read(db, page_key)
    if page is None:
        raise HTTPException(status_code=404, detail="page not found")
    data = wiki_page_to_dict(page)
    data["body"] = page.body
    graph = service.read_with_graph(db, page_key)
    data["outlinks"] = graph["outlinks"] if graph else []
    data["backlinks"] = graph["backlinks"] if graph else []
    return data


@router.get("/orient")
def orient(
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return service.orientation(db)


@router.get("/links")
def links(
    page_key: str,
    direction: str = "out",
    limit: int = 50,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    try:
        return {"items": service.follow_links(db, page_key, direction=direction, limit=limit)}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@router.post("/route")
def route(
    payload: WikiRouteRequest,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return service.route(db, payload.query, limit=payload.limit)


@router.get("/browse")
def browse(
    path_prefix: str = "",
    page_type: str = "",
    limit: int = 100,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return service.browse(db, path_prefix=path_prefix, page_type=page_type, limit=limit)


@router.get("/search")
def search_get(
    q: str = "",
    limit: int = 10,
    strategy: str = "",
    path_prefix: str = "",
    include_constraints: bool = False,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return {
        "items": [
            {
                "score": item["score"],
                "source": item["source"],
                "page_key": item["page_key"],
                "title": item["title"],
                "path": item["path"],
                "summary": item["summary"],
                "tags": item.get("tags", []),
                "page_type": item.get("page_type", ""),
                "confidence": item.get("confidence", 0.5),
                "match_reasons": item.get("match_reasons", []),
                "matched_fields": item.get("matched_fields", []),
                "snippet": item.get("snippet", ""),
                "next_actions": item.get("next_actions", []),
                "constraints": item.get("constraints", []),
                "page": wiki_page_to_dict(item["page"]),
            }
            for item in service.search(
                db,
                q,
                limit=limit,
                strategy=strategy,
                path_prefix=path_prefix,
                include_constraints=include_constraints,
            )
        ]
    }


@router.post("/search")
def search_post(
    payload: WikiSearchRequest,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return search_get(
        q=payload.query,
        limit=payload.limit,
        strategy=payload.strategy,
        path_prefix=payload.path_prefix,
        include_constraints=payload.include_constraints,
        db=db,
        service=service,
    )


@router.post("/compile")
def compile_wiki(
    payload: WikiCompileRequest | None = None,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
    jobs: BackgroundJobService = Depends(get_job_service),
    user: User = Depends(get_current_user),
) -> dict:
    payload = payload or WikiCompileRequest()
    if payload.enqueue:
        job = enqueue_background_job(
            db,
            task_name="wiki_compile",
            payload={},
            triggered_by=user.username,
            service=jobs,
        )
        return {"requested_by": user.username, "job": background_job_to_dict(job)}
    result = service.compile(db)
    return {"requested_by": user.username, **result}


@router.post("/lint")
def lint_wiki(
    payload: WikiLintRequest | None = None,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
    jobs: BackgroundJobService = Depends(get_job_service),
    user: User = Depends(get_current_user),
) -> dict:
    payload = payload or WikiLintRequest()
    if payload.enqueue:
        job = enqueue_background_job(db, task_name="wiki_lint", payload={}, triggered_by=user.username, service=jobs)
        return {"requested_by": user.username, "job": background_job_to_dict(job)}
    return {"requested_by": user.username, **service.lint(db)}


@router.post("/repair")
def repair_wiki(
    payload: WikiRepairRequest | None = None,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
    jobs: BackgroundJobService = Depends(get_job_service),
    llm: OpenAICompatibleClient = Depends(get_llm_client),
    user: User = Depends(get_current_user),
) -> dict:
    payload = payload or WikiRepairRequest()
    job_payload = {"apply_safe": payload.apply_safe, "error_ids": payload.error_ids}
    if payload.enqueue:
        job = enqueue_background_job(
            db,
            task_name="wiki_repair",
            payload=job_payload,
            triggered_by=user.username,
            service=jobs,
        )
        return {"requested_by": user.username, "job": background_job_to_dict(job)}
    return {
        "requested_by": user.username,
        **service.repair(db, apply_safe=payload.apply_safe, error_ids=payload.error_ids, llm=llm),
    }


@router.get("/health")
def health(
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return service.health(db)


@router.get("/errors")
def errors(
    status: str | None = "open",
    limit: int = 100,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return {"items": [wiki_error_to_dict(item) for item in service.error_book(db, status=status, limit=limit)]}
