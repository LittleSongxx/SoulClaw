---
title: SoulClaw Platform Wiki
page_key: index
type: architecture
tags:
  - soulclaw
  - platform
confidence: 0.8
summary: SoulClaw uses Markdown as the authoritative LLM Wiki source and rebuilds local SQLite page/link indexes from it.
---

# SoulClaw Platform Wiki

Markdown files in this directory are the authoritative source for LLM Wiki knowledge.
The compiler mirrors page metadata, links, compile status, and error-book records into the local database for page-index search and graph traversal.

## Principles

- Keep human-authored knowledge in Markdown.
- Treat the database as a rebuildable mirror, not the source of truth.
- Use links such as [[index]] to form an explicit knowledge graph.
