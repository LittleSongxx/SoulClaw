"""FastAPI entry point for the ZLAgent v2 platform runtime."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from loguru import logger

from . import __version__
from .api.admin import auth, control, dream, memory, platform, skills, tools, wiki
from .domain.memory import MemoryService
from .domain.platform import PlatformService
from .domain.skills import SkillService
from .domain.tools import ToolExecutor, ToolRegistry
from .domain.wiki import WikiService
from .infra.config import get_settings
from .infra.db import run_alembic_upgrade, session_scope
from .infra.events import RuntimeEventBus
from .infra.qdrant_index import QdrantHybridIndex
from .infra.redis_cache import build_redis_client
from .infra.security import ensure_admin_user
from .runtime.agent import AgentRuntime
from .runtime.cron import CronScheduler
from .runtime.dream import DreamRuntime
from .runtime.gateway import GatewayRuntimeManager
from .runtime.llm import OpenAICompatibleClient
from .runtime.mcp import MCPRuntimeManager


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = get_settings()
    settings.ensure_directories()
    logger.remove()
    logger.add(lambda msg: print(msg, end=""), level=settings.log_level.upper())
    logger.info("starting {} v{} ({})", settings.app_name, __version__, settings.environment)

    if settings.auto_migrate:
        run_alembic_upgrade(settings)

    events = RuntimeEventBus()
    with session_scope() as db:
        admin = ensure_admin_user(db, settings)
        logger.info("admin user ready: {}", admin.username)

    qdrant = QdrantHybridIndex(settings)
    qdrant.ensure_collections()
    redis_client = build_redis_client(settings)

    wiki_service = WikiService(settings=settings, qdrant=qdrant, events=events)
    memory_service = MemoryService(settings=settings, qdrant=qdrant, events=events)
    skill_service = SkillService(settings=settings, qdrant=qdrant, events=events)
    platform_service = PlatformService(events=events)
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
    tool_executor = ToolExecutor(tool_registry, events=events, platform=platform_service)
    llm_client = OpenAICompatibleClient(settings)
    agent_runtime = AgentRuntime(
        wiki=wiki_service,
        memory=memory_service,
        events=events,
        tools=tool_executor,
        registry=tool_registry,
        llm=llm_client,
    )
    gateway_runtime = GatewayRuntimeManager(agent=agent_runtime, events=events)
    tool_registry.install_gateway(gateway_runtime)
    dream_runtime = DreamRuntime(skills=skill_service, events=events)
    cron_scheduler = CronScheduler(agent=agent_runtime, events=events)

    app.state.settings = settings
    app.state.event_bus = events
    app.state.qdrant_index = qdrant
    app.state.redis_client = redis_client
    app.state.wiki_service = wiki_service
    app.state.memory_service = memory_service
    app.state.skill_service = skill_service
    app.state.platform_service = platform_service
    app.state.tool_registry = tool_registry
    app.state.tool_executor = tool_executor
    app.state.mcp_runtime = mcp_runtime
    app.state.llm_client = llm_client
    app.state.agent_runtime = agent_runtime
    app.state.gateway_runtime = gateway_runtime
    app.state.dream_runtime = dream_runtime
    app.state.cron_scheduler = cron_scheduler

    events.emit("runtime.started", {"version": __version__, "environment": settings.environment})
    cron_scheduler.schedule_missing()
    cron_scheduler.start()
    try:
        yield
    finally:
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
        allow_origins=["*"],
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
    app.include_router(dream.router)
    app.include_router(tools.router)
    app.include_router(platform.router)
    app.include_router(control.router)

    frontend_dist = Path("frontend/dist")
    if frontend_dist.exists():
        app.mount("/", StaticFiles(directory=str(frontend_dist), html=True), name="frontend")

    return app


app = create_app()
