# ZLAgent v2 Platform

ZLAgent v2 is a greenfield platform runtime for a personal AI agent. It uses Postgres as the system of record, Qdrant as the rebuildable hybrid retrieval index, Redis as an optional hot cache, and Markdown files as the authoritative LLM Wiki source.

## Local Development

Create the Python environment:

```bash
conda create -n zlagent python=3.11
conda activate zlagent
pip install -e ".[dev]"
```

Start backing services:

```bash
docker compose up postgres redis qdrant
```

Run the backend:

```bash
cp .env.example .env
uvicorn backend.app:app --host 127.0.0.1 --port 8020
```

The default admin login is controlled by `ZLAGENT_ADMIN_USERNAME` and `ZLAGENT_ADMIN_PASSWORD`.

## Architecture Notes

- `workspace/knowledge/wiki/**/*.md` is the LLM Wiki source of truth.
- Alembic owns relational migrations. The old `create_all + apply_lightweight_migrations` path is no longer active.
- `ZLAGENT_DATABASE_URL` is the canonical database setting. Legacy `DATABASE_URL` is read as a compatibility alias only.
- Qdrant collections are rebuildable mirrors: `zlagent_wiki_chunks`, `zlagent_memory_items`, and `zlagent_skill_chunks`.
- Management APIs are JWT protected except `/api/health`.
- Tool execution goes through the v2 registry and safety floor. Tool runs are persisted to `tool_runs` and mirrored as runtime events.
- Cron, MCP server definitions, Gateway connections, Dream review jobs, and approvals are managed through Postgres-backed control-plane APIs.
- Cron jobs are executed by the v2 Postgres-backed scheduler on startup. Each job stores `next_run_at`, `last_run_at`, `last_status`, `last_result`, and run/failure counts.
- MCP tools are discovered from enabled server definitions into cached `mcp__server__tool` registry entries. Stdio, SSE, and streamable HTTP transports use the Python MCP SDK.
- Gateway runtime supports auditable inbound turns and controlled outbound sends. The built-in `gateway_send` tool requires approval.
- Dream review creates pending evolution proposals from L2 error memories and failed tool runs. It never applies skill changes automatically.
- Legacy packages are retained only as migration source material. See `backend/legacy_readme.md`; do not add new active behavior to the old runtime path.

## Management APIs

- Auth: `/api/auth/login`, `/api/auth/me`, `/api/auth/logout`
- Wiki: `/api/wiki/pages`, `/api/wiki/search`, `/api/wiki/read`, `/api/wiki/compile`, `/api/wiki/health`, `/api/wiki/errors`
- Memory: `/api/memory`, `/api/memory/search`, `/api/memory/conflicts`, `/api/memory/probes`
- Skills: `/api/skills`, `/api/skills/scan`, `/api/skills/proposals`, `/api/skills/{skill_key}/test`, `/api/skills/{skill_key}/rollback`
- Dream: `/api/dream/run`
- Tools and runs: `/api/tools`, `/api/tools/{tool_name}/run`, `/api/runs`, `/api/runs/turn`
- Control plane: `/api/approvals`, `/api/cron`, `/api/mcp`, `/api/mcp/refresh`, `/api/gateways`, `/api/gateways/inbound`, `/api/gateways/send`, `/api/events`, `/api/audit`, `/api/settings`

## Verification

```bash
pytest
ruff check backend/infra backend/domain backend/api/admin backend/runtime tests
npm run build --prefix frontend
```
