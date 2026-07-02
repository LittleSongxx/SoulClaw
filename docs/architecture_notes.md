# Active Architecture Notes

The active backend implementation lives in:

- `backend/infra`
- `backend/domain`
- `backend/api/admin`
- `backend/runtime`
- `backend/worker`

The public FastAPI entry remains `backend.app:app`. New behavior should be added through these layers so Wiki, Memory, Skills, Tools, MCP, Gateway, Cron, Dream, Approval, and Proposal all share the same durable event and audit surface.

SoulClaw is currently a local-first personal Agent platform with Postgres as the full-runtime authority:

- `workspace_seed/` is the tracked default template for SOUL, USER, MEMORY, HEARTBEAT, Wiki, and Skills.
- `workspace/` is private runtime projection/draft space and is ignored by Git.
- Startup merges missing seed files into `workspace/` without overwriting user-edited files.
- Postgres is the full production path for long-lived state, graph/checkpoint state, vector retrieval, jobs, audit, and observability. SQLite remains only a test/lightweight fallback.
- Long-lived personal state is authoritative in structured tables: `core_context_blocks` for `soul/user/heartbeat` and `memories` for durable memory. Markdown SOUL/USER/MEMORY/HEARTBEAT files are generated projections and draft inputs only.
- LLM-Wiki content is intentionally Markdown-first. Database tables mirror page metadata, links, quality/errors, and search indexes; agents must read Wiki evidence through `wiki_read` and link traversal before relying on facts.
- Long-lived changes flow through evidence/proposal/apply. Dream, Heartbeat, Memory curation, Skills, and A2A artifacts produce proposals or evidence; they do not directly mutate durable personal state.
- Heavy Dream, Wiki, Skill, MCP, A2A sync, and Heartbeat work runs through background jobs instead of the online request path.
