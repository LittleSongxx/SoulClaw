# Active Backend Architecture

The active backend implementation lives in:

- `backend/infra`
- `backend/domain`
- `backend/api/admin`
- `backend/runtime`

The public FastAPI entry remains `backend.app:app`. New behavior should be added through these layers so Wiki, Memory, Skills, Tools, MCP, Gateway, Cron, Dream, Approval, and Proposal all share the same durable event and audit surface.
