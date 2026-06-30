"""Factory helpers for worker-side service graphs."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.a2a import A2AService
from backend.domain.jobs import BackgroundJobService
from backend.domain.memory import MemoryService
from backend.domain.platform import PlatformService
from backend.domain.skills import SkillService
from backend.domain.wiki import WikiService
from backend.domain.workspace import WorkspaceService
from backend.infra.config import get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.resilience import ResilienceManager
from backend.runtime.a2a import A2ARuntimeManager
from backend.runtime.dream import DreamRuntime
from backend.runtime.heartbeat import HeartbeatRuntime
from backend.runtime.llm import OpenAICompatibleClient
from backend.runtime.mcp import MCPRuntimeManager


@dataclass(frozen=True)
class WorkerServices:
    events: RuntimeEventBus
    wiki: WikiService
    memory: MemoryService
    skills: SkillService
    dream: DreamRuntime
    heartbeat: HeartbeatRuntime
    jobs: BackgroundJobService
    platform: PlatformService
    mcp: MCPRuntimeManager
    a2a: A2ARuntimeManager
    llm: OpenAICompatibleClient


def build_worker_services() -> WorkerServices:
    settings = get_settings()
    events = RuntimeEventBus()
    resilience = ResilienceManager(events=events)
    workspace = WorkspaceService(settings=settings, events=events)
    workspace.ensure_files()
    wiki = WikiService(settings=settings, events=events)
    memory = MemoryService(settings=settings, events=events, workspace=workspace)
    skills = SkillService(settings=settings, events=events)
    a2a_service = A2AService(events=events)
    return WorkerServices(
        events=events,
        wiki=wiki,
        memory=memory,
        skills=skills,
        dream=DreamRuntime(skills=skills, events=events),
        heartbeat=HeartbeatRuntime(workspace=workspace, skills=skills, events=events),
        jobs=BackgroundJobService(events=events),
        platform=PlatformService(events=events),
        mcp=MCPRuntimeManager(
            events=events,
            discovery_timeout_seconds=settings.mcp_discovery_timeout_seconds,
            call_timeout_seconds=settings.mcp_call_timeout_seconds,
            resilience=resilience,
        ),
        a2a=A2ARuntimeManager(
            service=a2a_service,
            events=events,
            settings=settings,
            http_timeout_seconds=settings.a2a_http_timeout_seconds,
            resilience=resilience,
        ),
        llm=OpenAICompatibleClient(settings, events=events, resilience=resilience),
    )
