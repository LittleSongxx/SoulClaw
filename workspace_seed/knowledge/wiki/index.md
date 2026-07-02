---
title: SoulClaw Platform Wiki
page_key: index
type: architecture
tags:
  - soulclaw
  - platform
confidence: 0.8
summary: SoulClaw uses Markdown as the authoritative LLM-Wiki knowledge source and rebuilds database page/link indexes from it.
---

# SoulClaw Platform Wiki

Markdown files in this directory are the authoritative source for LLM-Wiki knowledge only.
The compiler mirrors page metadata, links, compile status, and error-book records into database tables for page-index search and graph traversal.
This Wiki authority is separate from long-lived personal state, which is authoritative in Postgres tables.

## Principles

- Keep human-authored knowledge in Markdown.
- Treat Wiki database rows as a rebuildable mirror of Markdown Wiki pages, not as the personal long-term-state authority.
- Use links such as [[index]] to form an explicit knowledge graph.
