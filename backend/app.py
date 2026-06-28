"""FastAPI entry point for the SoulClaw platform runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__
from .api.admin import a2a, auth, control, dream, memory, platform, skills, tools, wiki
from .domain.a2a import A2AService
from .domain.conversation import ConversationService
from .domain.evolution import EvolutionService
from .domain.jobs import BackgroundJobService
from .domain.memory import MemoryService
from .domain.platform import PlatformService
from .domain.skills import SkillService
from .domain.tools import ToolExecutor, ToolRegistry
from .domain.wiki import WikiService
from .domain.workspace import WorkspaceService
from .infra.config import get_settings
from .infra.db import run_alembic_upgrade, session_scope
from .infra.events import RuntimeEventBus
from .infra.redis_cache import build_redis_client
from .infra.security import ensure_admin_user
from .runtime.a2a import A2ARuntimeManager
from .runtime.agent import AgentRuntime
from .runtime.cron import CronScheduler
from .runtime.dream import DreamRuntime
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

    if settings.auto_migrate:
        run_alembic_upgrade(settings)

    events = RuntimeEventBus()
    with session_scope() as db:
        with events.bind_session(db):
            admin = ensure_admin_user(db, settings)
        logger.info("admin user ready: {}", admin.username)

    redis_client = build_redis_client(settings)

    workspace_service = WorkspaceService(settings=settings, events=events)
    workspace_service.ensure_files()
    wiki_service = WikiService(settings=settings, events=events)
    memory_service = MemoryService(settings=settings, events=events, workspace=workspace_service)
    skill_service = SkillService(settings=settings, events=events)
    conversation_service = ConversationService(events=events)
    a2a_service = A2AService(events=events)
    platform_service = PlatformService(events=events)
    job_service = BackgroundJobService(events=events)
    evolution_service = EvolutionService(
        skills=skill_service,
        memory=memory_service,
        wiki=wiki_service,
        workspace=workspace_service,
        events=events,
    )
    tool_registry = ToolRegistry(wiki=wiki_service, memory=memory_service, skills=skill_service, events=events)
    mcp_runtime = MCPRuntimeManager(
        events=events,
        discovery_timeout_seconds=settings.mcp_discovery_timeout_seconds,
        call_timeout_seconds=settings.mcp_call_timeout_seconds,
    )
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
    )
    tool_registry.install_a2a(a2a_runtime)
    tool_executor = ToolExecutor(tool_registry, events=events, platform=platform_service)
    llm_client = OpenAICompatibleClient(settings)
    agent_runtime = AgentRuntime(
        wiki=wiki_service,
        memory=memory_service,
        events=events,
        tools=tool_executor,
        registry=tool_registry,
        llm=llm_client,
        conversation=conversation_service,
        workspace=workspace_service,
        a2a=a2a_runtime,
    )
    gateway_runtime = GatewayRuntimeManager(agent=agent_runtime, events=events)
    tool_registry.install_gateway(gateway_runtime)
    dream_runtime = DreamRuntime(skills=skill_service, events=events)
    heartbeat_runtime = HeartbeatRuntime(workspace=workspace_service, skills=skill_service, events=events)
    cron_scheduler = CronScheduler(agent=agent_runtime, dream=dream_runtime, jobs=job_service, events=events)

    app.state.settings = settings
    app.state.event_bus = events
    app.state.redis_client = redis_client
    app.state.workspace_service = workspace_service
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
                        instruction="Review HEARTBEAT.md active tasks and create pending proposals.",
                        metadata={"system_task": "heartbeat", "managed_by": "soulclaw", "task_name": "heartbeat_check"},
                        enabled=True,
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[heartbeat] startup cron ensure failed: {}", exc)
                    events.emit("heartbeat.bootstrap.failed", {"error": str(exc)}, severity="warning")
            if settings.a2a_bootstrap_weaver_enabled and settings.a2a_weaver_base_url:
                try:
                    a2a_service.upsert_connection(
                        db,
                        name="weaver-deep-research",
                        kind="weaver",
                        endpoint=settings.a2a_weaver_base_url,
                        config={
                            "base_url": settings.a2a_weaver_base_url,
                            "internal_api_key": settings.a2a_weaver_internal_api_key,
                            "auth_user_header": settings.a2a_weaver_auth_user_header,
                            "user_id": settings.a2a_weaver_user_id,
                            "skill_ids": ["deep-research"],
                        },
                        enabled=True,
                        status="pending",
                        capabilities=["deep-research", "research", "weaver"],
                        skills=[
                            {
                                "id": "deep-research",
                                "name": "Deep Research",
                                "description": "Delegate evidence-driven deep research to Weaver.",
                                "tags": ["research", "deep-research", "weaver"],
                            }
                        ],
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning("[a2a] startup Weaver connection ensure failed: {}", exc)
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

    app.include_router(auth.router)
    app.include_router(wiki.router)
    app.include_router(memory.router)
    app.include_router(skills.router)
    app.include_router(a2a.router)
    app.include_router(dream.router)
    app.include_router(tools.router)
    app.include_router(platform.router)
    app.include_router(control.router)

    frontend_dist = Path("frontend/dist")
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
