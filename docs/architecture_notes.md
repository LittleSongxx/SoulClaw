# Active Architecture Notes

The active backend implementation lives in:

- `backend/infra`
- `backend/domain`
- `backend/api/admin`
- `backend/runtime`
- `backend/worker`

The public FastAPI entry remains `backend.app:app`. New behavior should be added through these layers so Wiki, Memory, Skills, Tools, MCP, Gateway, Cron, Dream, Approval, and Proposal all share the same durable event and audit surface.

SoulClaw is currently local-first:

- `workspace_seed/` is the tracked default template for SOUL, USER, MEMORY, HEARTBEAT, Wiki, and Skills.
- `workspace/` is private runtime state and is ignored by Git.
- Startup merges missing seed files into `workspace/` without overwriting user-edited files.
- SQLite is the default local database; Postgres remains an optional deployment backend.
- Markdown files are the authoritative long-term content; database tables are indexes, job state, audit logs, and observability mirrors.
- Wiki search is page-index lookup. Wiki evidence must be read from Markdown pages through `wiki_read` and link traversal.
- Heavy Dream, Wiki, Skill, MCP, and Heartbeat work runs through background jobs instead of the online request path.
