---
title: ZLAgent Platform Wiki
page_key: index
type: architecture
tags:
  - zlagent
  - platform
confidence: 0.8
summary: ZLAgent uses Markdown as the authoritative LLM Wiki source and rebuilds Postgres and Qdrant indexes from it.
---

# ZLAgent Platform Wiki

Markdown files in this directory are the authoritative source for LLM Wiki knowledge.
The compiler mirrors page metadata, links, compile status, and error-book records into Postgres, then writes searchable chunks into Qdrant.

## Principles

- Keep human-authored knowledge in Markdown.
- Treat Postgres and Qdrant as rebuildable mirrors.
- Use links such as [[index]] to form an explicit knowledge graph.
