---
title: Wiki Schema
page_key: schema
type: schema
tags:
  - soulclaw
  - llm-wiki
confidence: 0.9
summary: Default writing and structure rules for the SoulClaw LLM-Wiki.
---

# Wiki Schema

This Wiki is the Markdown source of truth for durable, inspectable LLM-Wiki knowledge. It is separate from personal long-term state, which is governed by structured database state and reviewable proposals.

## Page Types

- `entities/`: people, projects, tools, organizations, and concrete objects.
- `concepts/`: reusable ideas, definitions, patterns, and principles.
- `comparisons/`: structured tradeoffs between alternatives.
- `queries/`: reusable research questions and investigation notes.
- `raw/`: copied source material or raw notes.
- `_archive/`: outdated pages kept for traceability.

## Frontmatter

Recommended fields:

```yaml
title: Human readable title
page_key: folder/page-key
type: concept
tags:
  - example
confidence: 0.7
summary: One sentence summary.
sources: []
contested: false
```

Use wikilinks like `[[entities/example]]` to make relationships explicit.
