"""Wiki management API."""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from backend.api.admin.deps import get_current_user, get_db, get_wiki_service
from backend.api.admin.serializers import wiki_error_to_dict, wiki_page_to_dict
from backend.domain.wiki import WikiService
from backend.infra.models import User

router = APIRouter(prefix="/api/wiki", tags=["wiki"], dependencies=[Depends(get_current_user)])


class WikiSearchRequest(BaseModel):
    query: str
    limit: int = 10


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
    return data


@router.get("/search")
def search_get(
    q: str = "",
    limit: int = 10,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return {
        "items": [
            {"score": item["score"], "source": item["source"], "page": wiki_page_to_dict(item["page"])}
            for item in service.search(db, q, limit=limit)
        ]
    }


@router.post("/search")
def search_post(
    payload: WikiSearchRequest,
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
) -> dict:
    return search_get(q=payload.query, limit=payload.limit, db=db, service=service)


@router.post("/compile")
def compile_wiki(
    db: Session = Depends(get_db),
    service: WikiService = Depends(get_wiki_service),
    user: User = Depends(get_current_user),
) -> dict:
    result = service.compile(db)
    return {"requested_by": user.username, **result}


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

