# SoulClaw

[English](README.en.md)

![Python](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)
![FastAPI](https://img.shields.io/badge/FastAPI-runtime-009688?logo=fastapi&logoColor=white)
![Postgres](https://img.shields.io/badge/Postgres%20%2B%20pgvector-state%20%2B%20retrieval-4169E1?logo=postgresql&logoColor=white)
![Celery](https://img.shields.io/badge/Celery-background%20jobs-37814A)
![A2A](https://img.shields.io/badge/A2A-multi--agent-5B5FC7)
![License](https://img.shields.io/badge/License-MIT-black)

SoulClaw 是一个 LangGraph-oriented 的长期个人 AI Agent 平台，也是一个 A2A 多智能体编排节点。它把会话连续性、Postgres 权威长期状态、Agent-native LLM-Wiki、Skills、工具/MCP/Gateway、统一策略审批、向量检索、Dream/Reflection、Heartbeat、后台任务队列和 A2A 委托放在同一个可观测控制台里。

长期状态以 Postgres 表为唯一权威来源：`core_context_blocks` 管理 `soul/user/heartbeat`，`memories` 管理长期记忆账本，变更通过 evidence/proposal/apply 流程落地。Markdown 文件只作为可读投影和草稿入口；搜索用于定位页面或记忆，使用事实前应通过 `wiki_read` 或 `memory_get` 读取原文。仓库模板位于 `workspace_seed/`，真实运行时投影位于本机私有 `workspace/`，启动时只补缺失文件，不覆盖已有个人数据。

A2A 与 MCP 的分工是：MCP 用于工具和数据源接入；A2A 用于 DeepResearch、文档项目、日程项目、代码编写这类粗粒度、可跟踪、可返回 artifacts 的外部 Agent 协作。SoulClaw 内置 A2A 1.0 runtime、Agent Card、JSON-RPC endpoint、任务/事件/artifact 持久化。

```mermaid
flowchart LR
    User[用户 / 控制台 / Gateway / A2A Client] --> Agent[Agent Runtime]
    Agent --> LLM[OpenAI-compatible LLM]
    Agent --> Conv[Session Messages / Summary]
    Agent --> Core[(core_context_blocks / memories)]
    Core --> Files[Generated SOUL.md / USER.md / MEMORY.md / HEARTBEAT.md projections]
    Agent --> WikiTools[wiki_orient / wiki_search / wiki_read / wiki_follow_links]
    WikiTools --> Wiki[Markdown LLM-Wiki]
    Wiki --> PG[(Postgres state + pgvector)]
    Agent --> Tools[Tool Registry]
    Tools --> MCP[MCP Tools]
    Tools --> Gateway[Gateway Runtime]
    Tools --> Approval[Approval]
    Agent --> A2A[A2A Runtime]
    A2A --> Remote[Remote A2A 1.0 Agents]
    A2A --> Artifacts[A2A Tasks / Events / Artifacts]
    Dream[Dream / Reflection] --> Queue[Celery + Redis]
    Heartbeat[Heartbeat] --> Queue
    Scheduler[Scheduler] --> Queue
    Queue --> Worker[Worker]
    Worker --> Proposal[Pending Proposal]
    Proposal --> Human[人工 Apply / Reject]
    Human --> Core
    Human --> Wiki
    Human --> Skills
```

## 功能概览

| 能力 | 说明 |
|---|---|
| 会话连续性 | `session_messages` 保存 user/assistant/tool 消息，`session_summaries` 保存滚动摘要 |
| 结构化长期状态 | `core_context_blocks` 管理 soul/user/heartbeat，`memories` 管理长期记忆；Markdown 只作为投影和草稿入口 |
| LLM-Wiki | Markdown 是事实源；数据库表保存页面、链接、Error Book、编译状态和健康信息 |
| Wiki 工具 | `wiki_orient` 看 schema/index/log/page map；`wiki_search` 查页面索引；`wiki_read` 读完整页面；`wiki_follow_links` 沿链接遍历 |
| Memory | `memory_search` 定位记忆，`memory_get` 读取记忆；创建、验证、归档、替换记忆都会先生成待审批 proposal |
| Skills | 扫描、lint、proposal apply/reject、历史记录和 rollback |
| Tools/MCP/Gateway | 工具统一注册和审计；高风险工具走 Approval；Gateway 支持 inbound/send/HMAC/heartbeat 状态 |
| A2A 多智能体 | 发布 A2A 1.0 Agent Card；支持 JSON-RPC `SendMessage`、`SendStreamingMessage`、`GetTask`、`CancelTask`、`SubscribeToTask`、`ListTasks`；保存连接、任务、事件和 artifacts |
| DeepResearch 委托 | 默认 SoulSearcher 连接名 `soulsearcher-deep-research`；通过 A2A 1.0 `SendStreamingMessage` 委托研究任务 |
| Dream/Reflection | 进入 Celery 队列执行，生成 pending proposals，不直接改文件 |
| Heartbeat | 定时读取结构化 heartbeat block 的任务，生成待审批提案或记录 skipped |
| 后台任务 | 轻量模式下可由 API 进程触发；长期运行时可启用 Celery/Redis 执行 `dream_review`、`heartbeat_check`、`wiki_compile`、`wiki_lint`、`skill_scan`、`mcp_refresh` |
| 安全治理 | proposal apply/reject、工具审批、workspace 文件修改、cron/mcp/gateway/a2a 写操作都会进入审计 |

## A2A 多智能体

SoulClaw 会作为 orchestrator，把复杂任务委托给独立 Agent，而不是把外部 Agent 当作进程内函数。它提供两类接口：

```text
GET  /.well-known/agent-card.json
GET  /api/a2a/card
POST /api/a2a
```

`POST /api/a2a` 是公开 A2A 1.0 JSON-RPC endpoint，支持：

```text
SendMessage
SendStreamingMessage
GetTask
ListTasks
CancelTask
SubscribeToTask
GetExtendedAgentCard
```

当 `SOULCLAW_A2A_REQUIRE_PUBLIC_AUTH=true` 时，`POST /api/a2a` 始终要求
`Authorization: Bearer <SOULCLAW_A2A_PUBLIC_API_KEY>` 或 `X-SoulClaw-A2A-Key`；
即使 `SOULCLAW_PUBLIC_BASE_URL` 为空也不会绕过。只在本地匿名 JSON-RPC 测试时显式设置
`SOULCLAW_A2A_REQUIRE_PUBLIC_AUTH=false`。

管理端接口需要登录：

```text
GET  /api/a2a/connections
POST /api/a2a/connections
POST /api/a2a/connections/{connection_name}/discover
POST /api/a2a/delegate
GET  /api/a2a/tasks
GET  /api/a2a/tasks/{task_id}
POST /api/a2a/tasks/sync-active
POST /api/a2a/tasks/{task_id}/sync
POST /api/a2a/tasks/{task_id}/cancel
POST /api/a2a/tasks/{task_id}/resume-remote-approval
GET  /api/a2a/tasks/{task_id}/events?after_sequence=
POST /api/a2a/callbacks/soulsearcher
```

SoulClaw 在 A2A 链路里是 Supervisor 和审批入口：DeepResearch 这类长程任务可以保持
`working`、`input-required`、`auth-required`、`stalled` 等远端状态，不会因为一次 HTTP 等待超时就被当成失败。
远端 SoulSearcher 返回 `input-required/auth-required` 时，SoulClaw 会创建
`subject_type="a2a_remote_hitl"` 的本地 Approval；用户 approve/edit/reject/respond 后，
SoulClaw 会用同一个远端 `taskId/contextId` 发送 follow-up message，让 SoulSearcher 从原 LangGraph
checkpoint 继续，而不是重新启动研究。SoulSearcher 的最终报告应作为 A2A Artifact 返回；status message
只用于进度、审批提示或错误摘要。所有远端 artifacts 会保存为 evidence，不会自动写入长期记忆，长期状态仍走
curator/proposal/apply。

远端状态同步有三条路径：SoulSearcher webhook 主动回调、控制台单任务 `sync` 手动拉取，以及
`POST /api/a2a/tasks/sync-active` 创建的 `a2a_sync` 后台任务。后台任务会扫描
`submitted/working/input-required/auth-required/stalled` 且已有 remote task id 的任务，调用远端 `GetTask`
同步状态、artifact、错误 envelope 和远端 HITL。

默认 SoulSearcher A2A 1.0 DeepResearch 配置：

```env
SOULCLAW_A2A_BOOTSTRAP_SOULSEARCHER_ENABLED=true
SOULCLAW_A2A_SOULSEARCHER_BASE_URL=http://127.0.0.1:8001
SOULCLAW_A2A_SOULSEARCHER_INTERNAL_API_KEY=
SOULCLAW_A2A_SOULSEARCHER_AUTH_USER_HEADER=X-SoulSearcher-User
SOULCLAW_A2A_SOULSEARCHER_USER_ID=soulclaw
SOULCLAW_A2A_POLL_INTERVAL_SECONDS=5
SOULCLAW_A2A_STALLED_TIMEOUT_SECONDS=900
SOULCLAW_A2A_CALLBACK_PUBLIC_URL=
SOULCLAW_A2A_CALLBACK_SECRET=
SOULCLAW_A2A_LIVE_EVENT_IDLE_SECONDS=30
```

SoulClaw 发往 SoulSearcher 的 A2A metadata 会固定携带 `soulclaw_task_id`、
`client_request_id/idempotency_key`、`user_id`、`session_id`、`turn_id`、`capability`、
`callback_url` 和 callback token 信息。HTTP retry 只适合带幂等 key 的请求；没有幂等 key 的长程启动请求不应自动重放。

低风险读取/研究类任务可以自动委托；代码写入、日程修改、外部发送、文档写入等高风险 capability 会通过现有 Approval 机制拦截。

## LLM-Wiki 检索方式

默认主路径是 Agent 自主检索和读取：

```text
wiki_orient -> wiki_search 页面索引 -> wiki_read 页面原文 -> wiki_follow_links 多跳 -> 回答
```

`wiki_search` 不返回 chunk、snippet 或 `candidate_only`，只返回页面级索引信息。Agent 使用 Wiki 事实前应调用 `wiki_read`；如果只搜索/遍历但没有读取页面，运行时会记录 `wiki.retrieval_guard.warning`。

推荐 Wiki 目录运行在 `workspace/knowledge/wiki/`，默认模板在 `workspace_seed/knowledge/wiki/`：

```text
workspace_seed/knowledge/wiki/
  SCHEMA.md
  index.md
  log.md
  raw/
  entities/
  concepts/
  comparisons/
  queries/
  _archive/
```

## 长期状态与投影

仓库包含一组投影模板：

```text
workspace_seed/
  SOUL.md
  USER.md
  HEARTBEAT.md
  memory/
    MEMORY.md
  knowledge/wiki/
  skills/
```

启动后会 merge 到本机私有运行目录，并由结构化状态服务刷新为可读投影：

```text
workspace/
  SOUL.md
  USER.md
  HEARTBEAT.md
  memory/
    MEMORY.md
  knowledge/wiki/
  skills/
```

这些 Markdown 文件不是权威状态，只是 `core_context_blocks` 与 `memories` 的可读投影。手工修改会通过导入草稿 API 变成 proposal，不能绕过审批直接污染长期状态。`workspace/` 默认被 Git 忽略，适合保存本机投影和草稿。

## 快速启动

### Conda 本地开发

本地开发环境使用 conda 环境 `soulclaw`：

```bash
conda activate soulclaw
python -m pip install -e ".[dev]"
PYTHONPATH=. uvicorn backend.app:app --host 127.0.0.1 --port 8020
```

前端开发：

```bash
npm install --prefix frontend
npm run dev --prefix frontend
```

启动 worker：

```bash
conda run -n soulclaw env PYTHONPATH=. celery -A backend.worker.celery_app.celery_app worker --loglevel=INFO --concurrency=1
```

启动 scheduler：

```bash
conda run -n soulclaw env PYTHONPATH=. python -m backend.worker.scheduler
```

### Docker Compose

```bash
cp .env.example .env
docker compose up -d --build
```

默认服务：

| 服务 | 用途 |
|---|---|
| `soulclaw` | FastAPI + 前端控制台 |
| `soulclaw-postgres` | Postgres + pgvector，保存状态、审计、运行图和语义向量 |

本地需要后台队列时再显式启用 background profile：

```bash
docker compose --profile background up -d --build
```

| 可选服务 | 用途 |
|---|---|
| `soulclaw-worker` | Celery worker |
| `soulclaw-scheduler` | 扫描 `cron_jobs` 并 enqueue 到期任务 |
| `soulclaw-redis` | Celery broker/result backend |
| `soulclaw-postgres` | 默认状态数据库和向量数据库 |

如果只想单独启动 Postgres：

```bash
docker compose up -d postgres
```

完整运行模式默认使用 Postgres + pgvector；SQLite 只保留给测试或显式关闭向量检索的轻量降级路径。Postgres 使用正常 Alembic migration 链；SQLite 兼容入口仍在 app startup / `run_alembic_upgrade()` 中做 schema reconcile 后 stamp 到 head。项目目录是：

```text
/home/song/code/Agent/assistant/SoulClaw
```

打开：

- 控制台：<http://localhost:8020>
- 健康检查：<http://localhost:8020/api/health>
- Agent Card：<http://localhost:8020/.well-known/agent-card.json>

### 个人服务器生产部署

生产部署使用 Postgres + Redis + app + worker + scheduler，作为长期个人服务器形态。生产 app 端口默认只绑定 `127.0.0.1:8020`，建议放在外部 Caddy、Nginx 或 Cloudflare Tunnel 后面处理 TLS、域名和公网访问控制。

```bash
cp .env.production.example .env.production
# 编辑 .env.production，替换所有密码、JWT secret、CORS、公开 URL 和 A2A 公网鉴权值
docker compose --env-file .env.production -f docker-compose.prod.yml up -d --build
```

生产 compose 会强制要求关键变量，例如 `SOULCLAW_ADMIN_PASSWORD`、`SOULCLAW_JWT_SECRET`、`SOULCLAW_POSTGRES_PASSWORD`、`SOULCLAW_CORS_ORIGINS`、`SOULCLAW_PUBLIC_BASE_URL` 和 `SOULCLAW_A2A_PUBLIC_API_KEY`，并显式要求 Redis 可用。如果生产环境配置成 SQLite，后端会拒绝启动。

默认开发账号：

```env
SOULCLAW_ADMIN_USERNAME=admin
SOULCLAW_ADMIN_PASSWORD=soulclaw-admin
```

生产环境必须修改 `SOULCLAW_JWT_SECRET`、`SOULCLAW_ADMIN_PASSWORD`，并显式设置 CORS。

## 关键 API

```text
POST    /api/auth/login
GET     /api/health
GET     /api/health/live
GET     /api/health/ready

GET     /api/context/blocks
GET     /api/context/projections
POST    /api/context/blocks/{soul|user|heartbeat}/proposals
GET     /api/workspace/files/{soul|user|memory|heartbeat}                 # generated projections
POST    /api/workspace/files/{soul|user|memory|heartbeat}/import-draft    # imports draft as proposal

GET     /api/wiki/orient
GET     /api/wiki/search?q=...
POST    /api/wiki/search
GET     /api/wiki/read?page_key=...
POST    /api/wiki/compile
POST    /api/wiki/lint

GET     /api/memory
POST    /api/memory
POST    /api/memory/search
GET     /api/memory/get?memory_id=...

GET     /api/tools
POST    /api/tools/{tool_name}/run
GET     /api/approvals
POST    /api/approvals/{approval_id}/approve-and-run
POST    /api/approvals/{approval_id}/resume-turn

GET     /api/agent-runs
GET     /api/runs/{run_id}/graph
GET     /api/vector/status
POST    /api/vector/rebuild
GET     /api/policy/status
GET     /api/policy/rules
PUT     /api/policy/rules/{rule_id}

GET     /api/a2a/connections
POST    /api/a2a/delegate
GET     /api/a2a/tasks
GET     /api/a2a/tasks/{task_id}
POST    /api/a2a/tasks/sync-active
POST    /api/a2a/tasks/{task_id}/sync
POST    /api/a2a/tasks/{task_id}/cancel
POST    /api/a2a/tasks/{task_id}/resume-remote-approval
GET     /api/a2a/tasks/{task_id}/events?after_sequence=
POST    /api/a2a/callbacks/soulsearcher
POST    /api/a2a

GET     /api/mcp
POST    /api/mcp/refresh
GET     /api/gateways/status
POST    /api/gateways/inbound
POST    /api/gateways/send

GET     /api/jobs
POST    /api/jobs/{job_id}/cancel
POST    /api/heartbeat/run
GET     /api/heartbeat/status
GET     /api/evolution/proposals
POST    /api/evolution/proposals
POST    /api/evolution/proposals/{id}/apply
POST    /api/evolution/proposals/{id}/reject
```

## 验证

```bash
conda run -n soulclaw env PYTHONPATH=. pytest -q
conda run -n soulclaw env PYTHONPATH=. ruff check backend tests
npm ci --prefix frontend
npm run build --prefix frontend
docker build -t soulclaw:local .
docker compose --env-file .env.production.example -f docker-compose.prod.yml config
```

CI 使用同一组质量门禁：Python 3.11 后端测试、ruff、Node 20 前端构建、Docker build，以及 Postgres/pgvector 迁移验证；SQLite 仅覆盖测试兼容路径。

## 参考

- A2A specification: <https://a2a-protocol.org/latest/specification/>
- A2A and MCP: <https://a2a-protocol.org/latest/topics/a2a-and-mcp/>
- Agent Discovery: <https://a2a-protocol.org/latest/topics/agent-discovery/>
- A2A Python SDK: <https://github.com/a2aproject/a2a-python>
- LLM-Wiki paper: <https://arxiv.org/abs/2605.25480>
- Hermes LLM-Wiki skill: <https://github.com/NousResearch/hermes-agent/blob/main/skills/research/llm-wiki/SKILL.md>
- nanobot: <https://github.com/HKUDS/nanobot>
- Celery periodic tasks: <https://docs.celeryq.dev/en/stable/userguide/periodic-tasks.html>
