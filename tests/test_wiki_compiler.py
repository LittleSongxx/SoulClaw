from __future__ import annotations

import uuid
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from backend.domain.wiki import WikiService, normalize_page_key, parse_markdown_page
from backend.domain.wiki_eval import evaluate_wiki_dataset
from backend.infra.models import Base, EvolutionProposal, WikiErrorBook, WikiLink, WikiPage


class FakeSettings:
    def __init__(self, root: Path) -> None:
        self.resolved_wiki_root = root


class FakeEvents:
    def __init__(self) -> None:
        self.events = []

    def emit(self, event_type, payload=None, **kwargs) -> None:
        self.events.append((event_type, payload or {}, kwargs))


class FakeScalarResult:
    def __init__(self, items) -> None:
        self.items = items

    def __iter__(self):
        return iter(self.items)

    def all(self):
        return self.items


class WikiGraphDB:
    def __init__(self, pages, links=None, errors=None) -> None:
        self.pages = pages
        self.links = links or []
        self.errors = errors or []
        self.objects = []

    def add(self, item) -> None:
        self.objects.append(item)

    def flush(self) -> None:
        for item in self.objects:
            if getattr(item, "id", None) is None:
                item.id = uuid.uuid4()

    def get(self, model, item_id):
        for item in self.objects:
            if isinstance(item, model) and item.id == item_id:
                return item
        return None

    def scalar(self, statement):
        text = str(statement)
        if "wiki_pages" in text:
            return self.pages[0] if self.pages else None
        return None

    def scalars(self, statement):
        text = str(statement)
        if "wiki_links" in text:
            return FakeScalarResult(self.links)
        if "wiki_pages" in text:
            return FakeScalarResult(self.pages)
        if "wiki_error_book" in text:
            return FakeScalarResult([*self.errors, *[item for item in self.objects if isinstance(item, WikiErrorBook)]])
        if "evolution_proposals" in text:
            return FakeScalarResult([item for item in self.objects if isinstance(item, EvolutionProposal)])
        return FakeScalarResult([])

    def execute(self, statement):
        del statement
        return None


def test_parse_markdown_page_frontmatter_and_links(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    page = root / "Architecture.md"
    page.write_text(
        """---
title: Architecture Note
page_key: architecture
type: design
aliases:
  - Arch
tags:
  - platform
confidence: 0.9
summary: A compact architecture page.
---

# Ignored Heading

This page links to [[Index|home]] and [[Missing Page]].
""",
        encoding="utf-8",
    )

    parsed = parse_markdown_page(page, root)

    assert parsed.page_key == "architecture"
    assert parsed.title == "Architecture Note"
    assert parsed.page_type == "design"
    assert parsed.aliases == ["Arch"]
    assert parsed.tags == ["platform"]
    assert parsed.confidence == 0.9
    assert parsed.links == [("index", "home"), ("missing-page", "Missing Page")]


def test_page_key_normalization() -> None:
    assert normalize_page_key("Folder/My Page.md") == "folder/my-page"


def test_wiki_search_returns_page_index_shape(tmp_path: Path) -> None:
    page = WikiPage(page_key="architecture", title="Architecture", path="Architecture.md", summary="Summary", body="Body")
    service = WikiService(settings=FakeSettings(tmp_path))

    result = service.search(WikiGraphDB([page]), "Architecture")

    assert result[0]["source"] == "page_index"
    assert result[0]["page_key"] == "architecture"
    assert result[0]["summary"] == "Summary"
    assert "chunk_index" not in result[0]


def test_wiki_structured_ranker_prioritizes_alias_over_body_match(tmp_path: Path) -> None:
    alias_page = WikiPage(
        page_key="entities/agent-card",
        title="Agent Card",
        path="entities/agent-card.md",
        summary="A2A discovery metadata.",
        body="Protocol metadata.",
        aliases=["Card"],
        tags=["a2a"],
        confidence=0.8,
    )
    body_page = WikiPage(
        page_key="notes/random",
        title="Random",
        path="notes/random.md",
        summary="Loose note.",
        body="This page mentions Agent Card in a long body only.",
        confidence=0.9,
    )
    service = WikiService(settings=FakeSettings(tmp_path))

    result = service.search(WikiGraphDB([body_page, alias_page]), "Card")

    assert result[0]["page_key"] == "entities/agent-card"
    assert "aliases" in result[0]["matched_fields"]
    assert "chunk_index" not in result[0]


def test_wiki_search_handles_null_confidence(tmp_path: Path) -> None:
    page = WikiPage(page_key="architecture", title="Architecture", path="architecture.md", summary="Architecture", body="")
    page.confidence = None
    service = WikiService(settings=FakeSettings(tmp_path))

    result = service.search(WikiGraphDB([page]), "Architecture")

    assert result[0]["page_key"] == "architecture"
    assert result[0]["confidence"] == 0.0


def test_wiki_route_and_browse_follow_llm_wiki_strategies(tmp_path: Path) -> None:
    pages = [
        WikiPage(page_key="index", title="Index", path="index.md", summary="Root map", body=""),
        WikiPage(page_key="entities/alice", title="Alice", path="entities/alice.md", summary="Alice profile", body=""),
        WikiPage(page_key="concepts/memory", title="Memory", path="concepts/memory.md", summary="Memory concept", body=""),
    ]
    service = WikiService(settings=FakeSettings(tmp_path))
    (tmp_path / "index.md").write_text("# Index\n\n- [[entities/alice]] Alice", encoding="utf-8")
    db = WikiGraphDB(pages)

    assert service.route(db, "Alice")["strategy"] == "search_first"
    assert service.route(db, "列出所有 Wiki 页面")["strategy"] == "browse_first"
    assert service.route(db, "比较 Alice 和 Memory 的关系")["strategy"] == "bridge"
    browse = service.browse(db, path_prefix="entities")

    assert [item["page_key"] for item in browse["items"]] == ["entities/alice"]


def test_wiki_sufficiency_detects_fan_in_and_error_book_constraints(tmp_path: Path) -> None:
    page = WikiPage(page_key="architecture", title="Architecture", path="architecture.md", summary="Architecture", body="Architecture note", confidence=0.2)
    error = WikiErrorBook(
        error_type="low_confidence",
        page_key="architecture",
        root_cause="Page confidence is low.",
        constraint="Verify before relying on this page.",
        constraint_rule="Do not cite without verification.",
        verification_method="Read another supporting page.",
        status="open",
        payload={},
    )
    service = WikiService(settings=FakeSettings(tmp_path))

    result = service.sufficiency_check(
        WikiGraphDB([page], errors=[error]),
        claim="Architecture",
        read_pages=["architecture"],
        required_fan_in=2,
    )

    assert result["sufficient"] is False
    gap_types = {item["type"] for item in result["evidence_gaps"]}
    assert {"fan_in", "open_constraints"} <= gap_types


def test_wiki_eval_reports_page_level_metrics(tmp_path: Path) -> None:
    pages = [
        WikiPage(page_key="entities/alice", title="Alice", path="entities/alice.md", summary="Alice profile", body="Alice builds agents."),
        WikiPage(page_key="concepts/memory", title="Memory", path="concepts/memory.md", summary="Memory concept", body="Memory stores durable facts."),
    ]
    service = WikiService(settings=FakeSettings(tmp_path))
    rows = [
        {"query": "Alice", "expected_pages": ["entities/alice"], "expected_strategy": "search_first"},
        {"query": "列出所有 Wiki 页面", "expected_pages": [], "expected_strategy": "browse_first"},
    ]

    metrics = evaluate_wiki_dataset(service, WikiGraphDB(pages), rows)

    assert metrics["evidence_recall"] >= 0.9
    assert metrics["fan_in_success"] >= 0.9


def test_wiki_compile_builds_page_mirror_without_vector_chunks(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "Index.md").write_text("# Index\n\nHybrid search note.", encoding="utf-8")
    events = FakeEvents()
    service = WikiService(settings=FakeSettings(root), events=events)

    class CompileDB:
        def __init__(self) -> None:
            self.objects = []

        def add(self, item) -> None:
            self.objects.append(item)

        def flush(self) -> None:
            return None

        def execute(self, statement):
            del statement
            return None

        def scalar(self, statement):
            del statement
            return None

        def scalars(self, statement):
            del statement
            return FakeScalarResult([])

    result = service.compile(CompileDB())

    assert result["status"] == "ok"
    assert "qdrant_indexed" not in result
    assert "chunks" not in result
    assert any(item[0] == "wiki.compile" for item in events.events)


def test_wiki_compile_refreshes_sqlite_fts_when_available(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "index.md").write_text(
        "---\ntitle: Index\npage_key: index\nsummary: Root\n---\n\n# Index\n\nOrchid retrieval control.",
        encoding="utf-8",
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = WikiService(settings=FakeSettings(root))

    with Session(engine) as db:
        result = service.compile(db)
        status = service.fts_status(db)
        hits = service.search(db, "orchid")

    assert result["status"] == "ok"
    assert result["fts"]["backend"] == "sqlite"
    if status["available"]:
        assert status["indexed_pages"] == 1
        assert hits[0]["source"] in {"wiki_fts", "page_index"}
        assert hits[0]["snippet"]
    else:
        assert status["fallback"] is True


def test_wiki_repair_auto_applies_low_risk_canonical_files(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = WikiService(settings=FakeSettings(root))

    with Session(engine) as db:
        result = service.repair(db, apply_safe=True)

    assert result["proposals_applied"] >= 1
    assert (root / "SCHEMA.md").exists()
    assert (root / "index.md").exists()


def test_wiki_repair_keeps_high_risk_semantic_items_pending(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "SCHEMA.md").write_text("# Schema", encoding="utf-8")
    (root / "index.md").write_text("# Index\n\n- [[concepts/fragile]] Fragile", encoding="utf-8")
    (root / "log.md").write_text("# Log", encoding="utf-8")
    (root / "concepts").mkdir()
    (root / "concepts" / "fragile.md").write_text(
        "---\ntitle: Fragile\npage_key: concepts/fragile\nconfidence: 0.2\nsummary: Low confidence claim.\n---\n\n# Fragile\n\nLow confidence claim.",
        encoding="utf-8",
    )
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    service = WikiService(settings=FakeSettings(root))

    with Session(engine) as db:
        result = service.repair(db, apply_safe=True)

    assert result["proposals_applied"] == 0
    assert any(item["risk_level"] == "medium" for item in result["created"])


def test_wiki_repair_reuses_existing_error_proposal(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "SCHEMA.md").write_text("# Schema", encoding="utf-8")
    (root / "index.md").write_text("# Index", encoding="utf-8")
    (root / "log.md").write_text("# Log", encoding="utf-8")
    error = WikiErrorBook(
        error_type="insufficient_evidence",
        page_key="concepts/fragile",
        root_cause="The claim has no supporting Wiki evidence.",
        constraint="Read or create supporting pages before relying on this claim.",
        status="open",
        payload={"claim": "fragile claim"},
    )
    error.id = uuid.uuid4()
    proposal = EvolutionProposal(
        target_type="wiki",
        action="append_log",
        status="pending",
        payload={"error_id": str(error.id)},
        evidence={"source": "wiki_repair", "error": {"id": str(error.id)}},
    )
    proposal.id = uuid.uuid4()
    service = WikiService(settings=FakeSettings(root))

    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    with Session(engine) as db:
        db.add(error)
        db.add(proposal)
        db.flush()
        result = service.repair(db, apply_safe=False)

    assert result["proposals_created"] == 0
    assert result["skipped"][0]["reason"] == "existing repair proposal"


def test_wiki_orientation_and_lint_detect_llm_wiki_shape(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "SCHEMA.md").write_text("# Schema", encoding="utf-8")
    (root / "index.md").write_text("# Index\n\n- [[entities/alice]] Alice", encoding="utf-8")
    (root / "log.md").write_text("- boot", encoding="utf-8")
    (root / "entities").mkdir()
    (root / "entities" / "alice.md").write_text(
        "---\ntitle: Alice\npage_key: entities/alice\nconfidence: 0.2\nsources:\n  - raw/missing.txt\n---\n\n# Alice\n\nSee [[missing]].",
        encoding="utf-8",
    )
    page = WikiPage(page_key="entities/alice", title="Alice", path="entities/alice.md", summary="Alice", body="Body", confidence=0.2)
    service = WikiService(settings=FakeSettings(root), events=FakeEvents())
    db = WikiGraphDB([page])

    orientation = service.orientation(db)
    lint = service.lint(db)

    assert orientation["schema"] == "# Schema"
    assert "entities" in orientation["directories"]
    assert lint["ok"] is False
    error_types = {item["error_type"] for item in lint["items"]}
    assert {"broken_link", "low_confidence", "source_drift"} <= error_types


def test_wiki_follow_links_returns_neighbor_pages(tmp_path: Path) -> None:
    right = WikiPage(page_key="b", title="B", path="b.md", summary="B", body="")
    link = WikiLink(src_page_key="a", dst_page_key="b", status="resolved", anchor_text="B", metadata_json={})
    service = WikiService(settings=FakeSettings(tmp_path))

    result = service.follow_links(WikiGraphDB([right], [link]), "a")

    assert result[0]["dst_page_key"] == "b"
    assert result[0]["page"]["title"] == "B"


def test_wiki_proposal_append_log_is_applyable(tmp_path: Path) -> None:
    root = tmp_path / "wiki"
    root.mkdir()
    (root / "index.md").write_text("# Index", encoding="utf-8")
    proposal = EvolutionProposal(target_type="wiki", action="append_log", status="pending", payload={"entry": "hello"})
    proposal.id = uuid.uuid4()
    db = WikiGraphDB([])
    db.add(proposal)
    service = WikiService(settings=FakeSettings(root))

    applied = service.apply_proposal(db, proposal.id, actor="tester")

    assert applied.status == "applied"
    assert "hello" in (root / "log.md").read_text(encoding="utf-8")
