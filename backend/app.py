"""FastAPI entry point for the SoulClaw platform runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from loguru import logger
from starlette.middleware.base import BaseHTTPMiddleware

from . import __version__
from .api.admin import a2a, auth, context, control, dream, memory, platform, skills, tools, wiki
from .domain.a2a import A2AService
from .domain.conversation import ConversationService
from .domain.core_context import CoreContextService
from .domain.evolution import EvolutionService
from .domain.evolution_proposals import EvolutionProposalService
from .domain.jobs import BackgroundJobService
from .domain.memory import MemoryService
from .domain.memory_curator import MemoryCuratorService
from .domain.policy import ToolPolicyEngine
from .domain.platform import PlatformService
from .domain.runs import AgentRunService
from .domain.skills import SkillService
from .domain.tools import ToolExecutor, ToolRegistry
from .domain.vector import KnowledgeVectorService
from .domain.wiki import WikiService
from .domain.workspace import WorkspaceService
from .infra.config import get_settings
from .infra.db import get_engine, run_alembic_upgrade, session_scope
from .infra.events import RuntimeEventBus
from .infra.health import readiness_summary
from .infra.observability import Observability, ObservabilityMiddleware, setup_opentelemetry
from .infra.rate_limit import FixedWindowRateLimiter, RateLimitExceeded
from .infra.redis_cache import build_redis_client
from .infra.resilience import ResilienceManager
from .infra.security import ensure_admin_user
from .infra.trace import bind_trace_context, trace_id_from_traceparent, traceparent_from_trace_id
from .runtime.a2a import A2ARuntimeManager
from .runtime.agent import AgentRuntime
from .runtime.checkpoint import AgentCheckpointStore
from .runtime.cron import CronScheduler
from .runtime.dream import DreamRuntime
from .runtime.embedding import OpenAICompatibleEmbeddingClient
from .runtime.gateway import GatewayRuntimeManager
from .runtime.heartbeat import HeartbeatRuntime
from .runtime.llm import OpenAICompatibleClient
from .runtime.mcp import MCPRuntimeManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.validate_runtime_secrets()
    settings.ensure_directories()
    logger.remove()
    logger.add(lambda msg: print(msg, end=""), level=settings.log_level.upper())
    logger.info("starting {} v{} ({})", settings.app_name, __version__, settings.environment)
    if settings.vector_required and settings.database_url.startswith("sqlite"):
        raise RuntimeError("SOULCLAW_VECTOR_MODE=required needs Postgres + pgvector; set SOULCLAW_DATABASE_URL to Postgres or disable vectors for tests")

    if settings.auto_migrate:
        run_alembic_upgrade(settings)

    events = RuntimeEventBus()
    with session_scope() as db:
        with events.bind_session(db):
            admin = ensure_admin_user(db, settings)
        logger.info("admin user ready: {}", admin.username)

    redis_client = build_redis_client(settings)
    resilience = ResilienceManager(events=events, observability=getattr(app.state, "observability", None))
    rate_limiter = FixedWindowRateLimiter(settings=settings, redis_client=redis_client, events=events)

    workspace_service = WorkspaceService(settings=settings, events=events)
    workspace_service.ensure_files()
    embedding_client = OpenAICompatibleEmbeddingClient(settings, events=events, resilience=resilience, rate_limiter=rate_limiter)
    vector_service = KnowledgeVectorService(embeddings=embedding_client, settings=settings, events=events)
    policy_engine = ToolPolicyEngine(events=events)
    run_service = AgentRunService(settings=settings, events=events)
    checkpoint_store = AgentCheckpointStore(settings=settings, events=events)
    wiki_service = WikiService(settings=settings, events=events, vector=vector_service)
    core_context_service = CoreContextService(workspace=workspace_service, events=events)
    memory_service = MemoryService(settings=settings, events=events, vector=vector_service)
    skill_service = SkillService(settings=settings, events=events, vector=vector_service)
    evolution_proposal_service = EvolutionProposalService(skills=skill_service, events=events)
    memory_curator_service = MemoryCuratorService(
        memory=memory_service,
        core_context=core_context_service,
        proposals=evolution_proposal_service,
        events=events,
    )
    conversation_service = ConversationService(events=events)
    a2a_service = A2AService(events=events)
    platform_service = PlatformService(events=events)
    job_service = BackgroundJobService(events=events)
    evolution_service = EvolutionService(
        skills=skill_service,
        memory=memory_service,
        wiki=wiki_service,
        core_context=core_context_service,
    )
    with session_scope() as db:
        with events.bind_session(db):
            policy_engine.ensure_defaults(db)
    tool_registry = ToolRegistry(
        wiki=wiki_service,
        memory=memory_service,
        skills=skill_service,
        proposals=evolution_proposal_service,
        events=events,
        max_direct_tool_schemas=settings.tool_schema_direct_limit,
    )
    mcp_runtime = MCPRuntimeManager(
        events=events,
        discovery_timeout_seconds=settings.mcp_discovery_timeout_seconds,
        call_timeout_seconds=settings.mcp_call_timeout_seconds,
        resilience=resilience,
    )
    if settings.mcp_seed_on_startup:
        try:
            with session_scope() as db:
                with events.bind_session(db):
                    result = platform_service.import_mcp_seed(db, settings)
                    events.emit("mcp.seed.bootstrap.succeeded", result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp] seed import failed: {}", exc)
            events.emit("mcp.seed.bootstrap.failed", {"error": str(exc)}, severity="warning")
    if settings.mcp_refresh_on_startup:
        try:
            await mcp_runtime.refresh_all()
        except Exception as exc:  # noqa: BLE001
            logger.warning("[mcp] startup refresh failed: {}", exc)
            events.emit("mcp.startup_refresh.failed", {"error": str(exc)}, severity="warning")
    mcp_runtime.install_into_registry(tool_registry)
    a2a_runtime = A2ARuntimeManager(
        service=a2a_service,
        events=events,
        settings=settings,
        http_timeout_seconds=settings.a2a_http_timeout_seconds,
        resilience=resilience,
        platform=platform_service,
    )
    tool_registry.install_a2a(a2a_runtime)
    tool_executor = ToolExecutor(tool_registry, events=events, platform=platform_service, policy=policy_engine)
    llm_client = OpenAICompatibleClient(settings, events=events, resilience=resilience, rate_limiter=rate_limiter)
    agent_runtime = AgentRuntime(
        wiki=wiki_service,
        memory=memory_service,
        events=events,
        tools=tool_executor,
        registry=tool_registry,
        llm=llm_client,
        conversation=conversation_service,
        core_context=core_context_service,
        memory_curator=memory_curator_service,
        a2a=a2a_runtime,
        runs=run_service,
        checkpoints=checkpoint_store,
    )
    gateway_runtime = GatewayRuntimeManager(
        agent=agent_runtime,
        events=events,
        webhook_max_skew_seconds=settings.gateway_webhook_max_skew_seconds,
        webhook_nonce_cache_size=settings.gateway_webhook_nonce_cache_size,
        resilience=resilience,
    )
    tool_registry.install_gateway(gateway_runtime)
    dream_runtime = DreamRuntime(proposals=evolution_proposal_service, events=events)
    heartbeat_runtime = HeartbeatRuntime(core_context=core_context_service, proposals=evolution_proposal_service, events=events)
    cron_scheduler = CronScheduler(agent=agent_runtime, dream=dream_runtime, jobs=job_service, events=events)

    app.state.settings = settings
    app.state.event_bus = events
    app.state.resilience = resilience
    app.state.rate_limiter = rate_limiter
    app.state.redis_client = redis_client
    app.state.workspace_service = workspace_service
    app.state.embedding_client = embedding_client
    app.state.vector_service = vector_service
    app.state.policy_engine = policy_engine
    app.state.run_service = run_service
    app.state.checkpoint_store = checkpoint_store
    app.state.core_context_service = core_context_service
    app.state.memory_curator_service = memory_curator_service
    app.state.evolution_proposal_service = evolution_proposal_service
    app.state.wiki_service = wiki_service
    app.state.memory_service = memory_service
    app.state.skill_service = skill_service
    app.state.conversation_service = conversation_service
    app.state.a2a_service = a2a_service
    app.state.job_service = job_service
    app.state.platform_service = platform_service
    app.state.evolution_service = evolution_service
    app.state.tool_registry = tool_registry
    app.state.tool_executor = tool_executor
    app.state.mcp_runtime = mcp_runtime
    app.state.llm_client = llm_client
    app.state.a2a_runtime = a2a_runtime
    app.state.agent_runtime = agent_runtime
    app.state.gateway_runtime = gateway_runtime
    app.state.dream_runtime = dream_runtime
    app.state.heartbeat_runtime = heartbeat_runtime
    app.state.cron_scheduler = cron_scheduler

    events.emit("runtime.started", {"version": __version__, "environment": settings.environment})
    with session_scope() as db:
        with events.bind_session(db):
            if settings.bootstrap_wiki_on_startup:
                try:
                    result = wiki_service.compile(db)
                    events.emit("wiki.bootstrap.succeeded", result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[wiki] startup compile failed: {}", exc)
                    events.emit("wiki.bootstrap.failed", {"error": str(exc)}, severity="warning")
            if settings.bootstrap_skills_on_startup:
                try:
                    result = skill_service.scan(db)
                    events.emit("skills.bootstrap.succeeded", result)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[skills] startup scan failed: {}", exc)
                    events.emit("skills.bootstrap.failed", {"error": str(exc)}, severity="warning")
            try:
                core_context_service.ensure_defaults(db)
                core_context_service.refresh_projections(db)
            except Exception as exc:  # noqa: BLE001
                logger.warning("[context] startup projection refresh failed: {}", exc)
                events.emit("core_context.bootstrap.failed", {"error": str(exc)}, severity="warning")
            if settings.dream_review_enabled:
                try:
                    platform_service.ensure_system_cron_job(
                        db,
                        name="system-dream-review",
                        cron_expr=settings.dream_review_cron,
                        timezone=settings.dream_review_timezone,
                        instruction="Run Dream review and create pending improvement proposals.",
                        metadata={
                            "system_task": "dream_review",
                            "managed_by": "soulclaw",
                            "window_hours": settings.dream_review_window_hours,
                            "limit": settings.dream_review_limit,
                            "auto_apply": False,
                        },
                        enabled=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[dream] startup cron ensure failed: {}", exc)
                    events.emit("dream.bootstrap.failed", {"error": str(exc)}, severity="warning")
            if settings.heartbeat_enabled:
                try:
                    platform_service.ensure_system_cron_job(
                        db,
                        name="system-heartbeat",
                        cron_expr=settings.heartbeat_cron,
                        timezone=settings.heartbeat_timezone,
                        instruction="Review structured heartbeat tasks and create pending proposals.",
                        metadata={"system_task": "heartbeat", "managed_by": "soulclaw", "task_name": "heartbeat_check"},
                        enabled=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[heartbeat] startup cron ensure failed: {}", exc)
                    events.emit("heartbeat.bootstrap.failed", {"error": str(exc)}, severity="warning")
            if settings.a2a_bootstrap_soulsearcher_enabled and settings.a2a_soulsearcher_base_url:
                try:
                    a2a_service.upsert_connection(
                        db,
                        name="soulsearcher-deep-research",
                        kind="a2a",
                        endpoint=settings.a2a_soulsearcher_base_url,
                        config={
                            "base_url": settings.a2a_soulsearcher_base_url,
                            "internal_api_key": settings.a2a_soulsearcher_internal_api_key,
                            "auth_user_header": settings.a2a_soulsearcher_auth_user_header,
                            "user_id": settings.a2a_soulsearcher_user_id,
                            "accepted_output_modes": ["text/markdown", "text/html", "application/json"],
                            "callback_url": settings.a2a_callback_public_url,
                            "callback_token": settings.a2a_callback_secret,
                        },
                        enabled=True,
                        status="pending",
                        capabilities=["deep-research", "research", "soulsearcher"],
                        skills=[
                            {
                                "id": "deep-research",
                                "name": "Deep Research",
                                "description": "Delegate evidence-driven deep research to SoulSearcher over A2A 1.0.",
                                "tags": ["research", "deep-research", "soulsearcher"],
                            }
                        ],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[a2a] startup SoulSearcher connection ensure failed: {}", exc)
                    events.emit("a2a.bootstrap.failed", {"error": str(exc)}, severity="warning")
    cron_scheduler.schedule_missing()
    if settings.api_scheduler_enabled:
        cron_scheduler.start()
    try:
        yield
    finally:
        if settings.api_scheduler_enabled:
            await cron_scheduler.stop()
        events.emit("runtime.stopped", {"version": __version__})
        if redis_client is not None:
            redis_client.close()
        logger.info("shut down {} v{}", settings.app_name, __version__)


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title=settings.app_name, version=__version__, lifespan=lifespan)
    app.state.settings = settings
    observability = Observability(settings=settings)
    app.state.observability = observability
    otel_state = setup_opentelemetry(app, settings=settings, engine=get_engine())
    app.state.otel = otel_state
    app.add_middleware(ObservabilityMiddleware, observability=observability)
    app.add_middleware(TraceContextMiddleware)
    app.add_middleware(RateLimitMiddleware)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.cors_origins,
        allow_credentials=False,
        allow_methods=["*"],
        allow_headers=["*"],
    )

    @app.get("/api/health")
    def health() -> dict:
        return {"ok": True, "app": settings.app_name, "version": __version__}

    @app.get("/api/health/live")
    def live() -> dict:
        return {"ok": True, "app": settings.app_name, "version": __version__}

    @app.get("/api/health/ready")
    def ready() -> JSONResponse:
        summary = readiness_summary(settings, getattr(app.state, "redis_client", None))
        status_code = 200 if summary["ok"] else 503
        return JSONResponse({"app": settings.app_name, "version": __version__, **summary}, status_code=status_code)

    if settings.metrics_enabled:
        @app.get(settings.metrics_path)
        def metrics() -> JSONResponse:
            return app.state.observability.metrics_response()

    app.include_router(auth.router)
    app.include_router(context.router)
    app.include_router(wiki.router)
    app.include_router(memory.router)
    app.include_router(skills.router)
    app.include_router(a2a.router)
    app.include_router(dream.router)
    app.include_router(tools.router)
    app.include_router(platform.router)
    app.include_router(control.router)
    app.include_router(control.public_router)

    frontend_dist = Path("frontend/dist")
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


class TraceContextMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        trace_id = trace_id_from_traceparent(request.headers.get("traceparent", ""))
        request_id = request.headers.get("X-Request-ID", "")
        with bind_trace_context(trace_id=trace_id, request_id=request_id) as context:
            response = await call_next(request)
            response.headers["X-Request-ID"] = context.request_id
            if "traceparent" not in response.headers:
                response.headers["traceparent"] = traceparent_from_trace_id(context.trace_id)
            return response


class RateLimitMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        path = request.url.path
        settings = getattr(request.app.state, "settings", get_settings())
        if _rate_limit_exempt(path, settings):
            return await call_next(request)
        limiter = getattr(request.app.state, "rate_limiter", None)
        if limiter is None:
            return await call_next(request)
        policy, limit = _rate_limit_policy(path, settings)
        identity = _rate_limit_identity(request)
        try:
            decision = limiter.enforce(policy=policy, identity=identity, limit=limit)
        except RateLimitExceeded as exc:
            observability = getattr(request.app.state, "observability", None)
            if observability is not None:
                observability.record_rate_limit(policy=policy, allowed=False)
            return JSONResponse(
                {
                    "detail": "rate limit exceeded",
                    "code": "rate_limit_exceeded",
                    "policy": exc.policy,
                    "retry_after": exc.retry_after,
                },
                status_code=429,
                headers={"Retry-After": str(exc.retry_after)},
            )
        observability = getattr(request.app.state, "observability", None)
        if observability is not None:
            observability.record_rate_limit(policy=policy, allowed=decision.allowed)
        response = await call_next(request)
        response.headers["X-RateLimit-Limit"] = str(decision.limit)
        response.headers["X-RateLimit-Remaining"] = str(decision.remaining)
        response.headers["X-RateLimit-Reset"] = str(decision.reset_at)
        return response


def _rate_limit_exempt(path: str, settings) -> bool:
    return path in {"/api/health", "/api/health/live", "/api/health/ready", settings.metrics_path}


def _rate_limit_policy(path: str, settings):
    if path == "/api/runs/turn":
        return "turn", settings.rate_limit_turn_per_minute
    if path in {"/api/gateways/inbound", "/api/gateways/webhook"}:
        return "gateway", settings.rate_limit_gateway_per_minute
    return "admin", settings.rate_limit_admin_per_minute


def _rate_limit_identity(request: Request) -> str:
    auth = request.headers.get("Authorization", "")
    host = request.client.host if request.client else "unknown"
    return auth[-32:] if auth else host


app = create_app()
