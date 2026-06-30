"""Small offline evaluator for page-level LLM-Wiki retrieval."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session

from backend.domain.wiki import WikiService


def load_jsonl(path: Path) -> list[dict[str, Any]]:
    raw = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        loaded = json.loads(raw)
        if not isinstance(loaded, list):
            raise ValueError("wiki eval JSON dataset must be a list")
        return [dict(item) for item in loaded if isinstance(item, dict)]
    rows: list[dict[str, Any]] = []
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            rows.append(json.loads(stripped))
    return rows


def evaluate_wiki_dataset(service: WikiService, db: Session, rows: list[dict[str, Any]]) -> dict[str, Any]:
    total = max(1, len(rows))
    recall_sum = 0.0
    precision_sum = 0.0
    fan_in_hits = 0
    insufficient_hits = 0
    steps_sum = 0
    cases: list[dict[str, Any]] = []

    for row in rows:
        query = str(row.get("query") or "")
        expected_pages = {str(item) for item in row.get("expected_pages", [])}
        expected_strategy = str(row.get("expected_strategy") or "")
        expected_insufficient = bool(row.get("insufficient", False))
        route = service.route(db, query, limit=int(row.get("limit") or 10))
        strategy = str(route.get("strategy") or "")
        if strategy == "browse_first":
            hits = service.browse(db, limit=int(row.get("limit") or 10)).get("items", [])
        elif strategy == "bridge":
            searched = service.search(db, query, limit=int(row.get("limit") or 10), strategy=strategy)
            browsed = service.browse(db, limit=int(row.get("limit") or 10)).get("items", [])
            seen: set[str] = set()
            hits = []
            for item in [*searched, *browsed]:
                page_key = str(item.get("page_key") or "")
                if page_key and page_key not in seen:
                    hits.append(item)
                    seen.add(page_key)
        else:
            hits = service.search(db, query, limit=int(row.get("limit") or 10), strategy=strategy)
        hit_keys = [str(item["page_key"]) for item in hits]
        expected_count = max(1, len(expected_pages))
        relevant_hits = [key for key in hit_keys if key in expected_pages]
        recall = len(set(relevant_hits)) / expected_count if expected_pages else 1.0
        precision = len(relevant_hits) / max(1, len(hit_keys)) if expected_pages else 1.0
        read_pages = hit_keys[: max(1, int(route.get("required_fan_in") or 1))]
        sufficiency = service.sufficiency_check(
            db,
            claim=query,
            read_pages=read_pages,
            required_fan_in=int(route.get("required_fan_in") or 0),
            strategy=str(route.get("strategy") or ""),
        )
        fan_in_ok = expected_insufficient or len(read_pages) >= int(route.get("required_fan_in") or 0)
        insufficient_ok = (
            bool(route.get("strategy") == "insufficient" or not sufficiency["sufficient"])
            if expected_insufficient
            else bool(route.get("strategy") != "insufficient")
        )
        strategy_ok = not expected_strategy or route.get("strategy") == expected_strategy
        recall_sum += recall
        precision_sum += precision
        fan_in_hits += int(fan_in_ok and strategy_ok)
        insufficient_hits += int(insufficient_ok)
        steps_sum += len(route.get("recommended_steps") or [])
        cases.append(
            {
                "query": query,
                "expected_pages": sorted(expected_pages),
                "hit_pages": hit_keys,
                "strategy": route.get("strategy"),
                "expected_strategy": expected_strategy,
                "evidence_recall": round(recall, 4),
                "evidence_precision": round(precision, 4),
                "fan_in_success": fan_in_ok,
                "insufficient_detection": insufficient_ok,
            }
        )

    return {
        "cases": len(rows),
        "evidence_recall": round(recall_sum / total, 4),
        "evidence_precision": round(precision_sum / total, 4),
        "fan_in_success": round(fan_in_hits / total, 4),
        "insufficient_detection": round(insufficient_hits / total, 4),
        "avg_steps": round(steps_sum / total, 4),
        "case_results": cases,
    }
