from __future__ import annotations

from pathlib import Path

from backend.domain.wiki import chunk_text, normalize_page_key, parse_markdown_page


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


def test_chunk_text_overlaps() -> None:
    chunks = chunk_text("x" * 3000, max_chars=1000, overlap=100)

    assert len(chunks) == 4
    assert all(len(item) <= 1000 for item in chunks)

