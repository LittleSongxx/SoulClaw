from __future__ import annotations

import uuid
from pathlib import Path

from backend.domain.wiki import WikiService, normalize_page_key, parse_markdown_page
from backend.infra.models import EvolutionProposal, WikiLink, WikiPage


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
    def __init__(self, pages, links=None) -> None:
        self.pages = pages
        self.links = links or []
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
