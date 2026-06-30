"""Run the LLM-Wiki retrieval evaluator against the configured workspace."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from backend.domain.wiki import WikiService
from backend.domain.wiki_eval import evaluate_wiki_dataset, load_jsonl
from backend.infra.config import get_settings
from backend.infra.db import run_alembic_upgrade, session_scope


def main() -> None:
    parser = argparse.ArgumentParser(description="Evaluate page-level LLM-Wiki retrieval.")
    parser.add_argument("--dataset", required=True, help="Path to a JSONL dataset.")
    parser.add_argument("--compile", action="store_true", help="Compile the configured Wiki before evaluation.")
    parser.add_argument("--upgrade-db", action="store_true", help="Run Alembic migrations before evaluation.")
    parser.add_argument("--no-compile", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()

    settings = get_settings()
    if args.upgrade_db:
        run_alembic_upgrade(settings)
    service = WikiService(settings=settings)
    rows = load_jsonl(Path(args.dataset))
    with session_scope() as db:
        if args.compile and not args.no_compile:
            service.compile(db)
        metrics = evaluate_wiki_dataset(service, db, rows)
    print(json.dumps(metrics, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
