"""Factory helpers for worker-side service graphs."""

from __future__ import annotations

from dataclasses import dataclass

from backend.domain.a2a import A2AService
from backend.domain.core_context import CoreContextService
from backend.domain.evolution_proposals import EvolutionProposalService
from backend.domain.jobs import BackgroundJobService
from backend.domain.memory import MemoryService
from backend.domain.memory_curator import MemoryCuratorService
from backend.domain.platform import PlatformService
from backend.domain.skills import SkillService
from backend.domain.vector import KnowledgeVectorService
from backend.domain.wiki import WikiService
from backend.domain.workspace import WorkspaceService
from backend.infra.config import get_settings
from backend.infra.events import RuntimeEventBus
from backend.infra.resilience import ResilienceManager
from backend.runtime.a2a import A2ARuntimeManager
from backend.runtime.dream import DreamRuntime
from backend.runtime.heartbeat import HeartbeatRuntime
from backend.runtime.embedding import OpenAICompatibleEmbeddingClient
from backend.runtime.llm import OpenAICompatibleClient
from backend.runtime.mcp import MCPRuntimeManager
from backend.worker.guards import ensure_background_database


@dataclass(frozen=True)
class WorkerServices:
    events: RuntimeEventBus
    wiki: WikiService
    core_context: CoreContextService
    memory: MemoryService
    memory_curator: MemoryCuratorService
    proposals: EvolutionProposalService
    skills: SkillService
    dream: DreamRuntime
    heartbeat: HeartbeatRuntime
    jobs: BackgroundJobService
    platform: PlatformService
    embeddings: OpenAICompatibleEmbeddingClient
    vector: KnowledgeVectorService
    mcp: MCPRuntimeManager
    a2a: A2ARuntimeManager
    llm: OpenAICompatibleClient


def build_worker_services() -> WorkerServices:
    settings = get_settings()
    ensure_background_database(settings, component="worker")
    events = RuntimeEventBus()
    resilience = ResilienceManager(events=events)
    workspace = WorkspaceService(settings=settings, events=events)
    workspace.ensure_files()
    embeddings = OpenAICompatibleEmbeddingClient(settings, events=events, resilience=resilience)
    vector = KnowledgeVectorService(embeddings=embeddings, settings=settings, events=events)
    wiki = WikiService(settings=settings, events=events, vector=vector)
    core_context = CoreContextService(workspace=workspace, events=events)
    memory = MemoryService(settings=settings, events=events, vector=vector)
    skills = SkillService(settings=settings, events=events, vector=vector)
    proposals = EvolutionProposalService(skills=skills, events=events)
    memory_curator = MemoryCuratorService(memory=memory, core_context=core_context, proposals=proposals, events=events)
    a2a_service = A2AService(events=events)
    jobs = BackgroundJobService(events=events)
    platform = PlatformService(events=events)
    return WorkerServices(
        events=events,
        wiki=wiki,
        core_context=core_context,
        memory=memory,
        memory_curator=memory_curator,
        proposals=proposals,
        skills=skills,
        dream=DreamRuntime(proposals=proposals, events=events),
        heartbeat=HeartbeatRuntime(core_context=core_context, proposals=proposals, events=events),
        jobs=jobs,
        platform=platform,
        embeddings=embeddings,
        vector=vector,
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
            platform=platform,
        ),
        llm=OpenAICompatibleClient(settings, events=events, resilience=resilience),
    )
