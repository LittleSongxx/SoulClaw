"""Response serializers for SQLAlchemy entities."""

from __future__ import annotations

from datetime import datetime
from typing import Any


def dt(value: datetime | None) -> str | None:
    return value.isoformat() if value else None


def wiki_page_to_dict(page) -> dict[str, Any]:
    return {
        "id": str(page.id),
        "page_key": page.page_key,
        "title": page.title,
        "page_type": page.page_type,
        "path": page.path,
        "summary": page.summary,
        "aliases": page.aliases or [],
        "tags": page.tags or [],
        "confidence": page.confidence,
        "metadata": page.metadata_json or {},
        "updated_at": dt(page.updated_at),
    }


def wiki_error_to_dict(error) -> dict[str, Any]:
    return {
        "id": str(error.id),
        "error_type": error.error_type,
        "page_key": error.page_key,
        "root_cause": error.root_cause,
        "constraint": error.constraint,
        "constraint_rule": getattr(error, "constraint_rule", "") or "",
        "verification_method": getattr(error, "verification_method", "") or "",
        "source_refs": getattr(error, "source_refs", []) or [],
        "lifecycle_status": getattr(error, "lifecycle_status", error.status),
        "status": error.status,
        "payload": error.payload or {},
        "created_at": dt(error.created_at),
        "fixed_at": dt(error.fixed_at),
    }


def memory_to_dict(memory) -> dict[str, Any]:
    return {
        "id": str(memory.id),
        "kind": memory.kind,
        "content": memory.content,
        "source": memory.source,
        "status": getattr(memory, "status", "active"),
        "pinned": memory.pinned,
        "archived": memory.archived,
        "importance": memory.importance,
        "confidence": memory.confidence,
        "stability": memory.stability,
        "supersedes_id": str(memory.supersedes_id) if memory.supersedes_id else None,
        "superseded_by": str(getattr(memory, "superseded_by", "") or "") or None,
        "source_turn_id": memory.source_turn_id,
        "source_file_marker": getattr(memory, "source_file_marker", "") or "",
        "valid_from": dt(getattr(memory, "valid_from", None)),
        "valid_to": dt(getattr(memory, "valid_to", None)),
        "provenance": getattr(memory, "provenance", {}) or {},
        "metadata": memory.metadata_json or {},
        "created_at": dt(memory.created_at),
        "updated_at": dt(memory.updated_at),
        "last_verified_at": dt(memory.last_verified_at),
    }


def conflict_to_dict(conflict) -> dict[str, Any]:
    return {
        "id": str(conflict.id),
        "left_memory_id": str(conflict.left_memory_id),
        "right_memory_id": str(conflict.right_memory_id),
        "status": conflict.status,
        "reason": conflict.reason,
        "created_at": dt(conflict.created_at),
        "resolved_at": dt(conflict.resolved_at),
    }


def probe_to_dict(probe) -> dict[str, Any]:
    return {
        "id": str(probe.id),
        "question": probe.question,
        "expected": probe.expected,
        "status": probe.status,
        "last_result": probe.last_result or {},
        "created_at": dt(probe.created_at),
        "last_run_at": dt(probe.last_run_at),
    }


def session_message_to_dict(message) -> dict[str, Any]:
    return {
        "id": str(message.id),
        "session_id": message.session_id,
        "turn_id": message.turn_id,
        "role": message.role,
        "content": message.content,
        "metadata": message.metadata_json or {},
        "created_at": dt(message.created_at),
    }


def session_summary_to_dict(summary) -> dict[str, Any] | None:
    if summary is None:
        return None
    return {
        "id": str(summary.id),
        "session_id": summary.session_id,
        "summary": summary.summary,
        "summarized_message_count": summary.summarized_message_count,
        "metadata": summary.metadata_json or {},
        "created_at": dt(summary.created_at),
        "updated_at": dt(summary.updated_at),
    }


def skill_to_dict(skill) -> dict[str, Any]:
    return {
        "id": str(skill.id),
        "skill_key": skill.skill_key,
        "name": skill.name,
        "description": skill.description,
        "path": skill.path,
        "status": skill.status,
        "pinned": skill.pinned,
        "metadata": skill.metadata_json or {},
        "created_at": dt(skill.created_at),
        "updated_at": dt(skill.updated_at),
    }


def skill_file_to_dict(file) -> dict[str, Any]:
    return {
        "id": str(file.id),
        "skill_key": file.skill_key,
        "file_path": file.file_path,
        "checksum": file.checksum,
        "content": file.content,
        "updated_at": dt(file.updated_at),
    }


def proposal_to_dict(proposal) -> dict[str, Any]:
    return {
        "id": str(proposal.id),
        "target_type": proposal.target_type,
        "action": proposal.action,
        "status": proposal.status,
        "risk_level": proposal.risk_level,
        "payload": proposal.payload or {},
        "evidence": proposal.evidence or {},
        "before_snapshot": proposal.before_snapshot or {},
        "after_snapshot": proposal.after_snapshot or {},
        "result": proposal.result or {},
        "created_at": dt(proposal.created_at),
        "updated_at": dt(proposal.updated_at),
        "applied_at": dt(proposal.applied_at),
    }


def background_job_to_dict(job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "task_name": job.task_name,
        "queue_id": job.queue_id,
        "status": job.status,
        "payload": job.payload or {},
        "result": job.result or {},
        "error": job.error,
        "triggered_by": job.triggered_by,
        "cron_job_id": str(job.cron_job_id) if job.cron_job_id else None,
        "created_at": dt(job.created_at),
        "started_at": dt(job.started_at),
        "finished_at": dt(job.finished_at),
    }


def runtime_event_to_dict(event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "severity": event.severity,
        "session_id": event.session_id,
        "turn_id": event.turn_id,
        "payload": event.payload or {},
        "created_at": dt(event.created_at),
    }


def audit_event_to_dict(event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "actor_id": str(event.actor_id) if event.actor_id else None,
        "action": event.action,
        "target_type": event.target_type,
        "target_id": event.target_id,
        "payload": event.payload or {},
        "created_at": dt(event.created_at),
    }


def approval_to_dict(approval) -> dict[str, Any]:
    return {
        "id": str(approval.id),
        "status": approval.status,
        "subject_type": approval.subject_type,
        "subject_id": approval.subject_id,
        "payload": approval.payload or {},
        "turn_checkpoint": getattr(approval, "turn_checkpoint", {}) or {},
        "original_tool_call": getattr(approval, "original_tool_call", {}) or {},
        "allowed_decisions": getattr(approval, "allowed_decisions", []) or [],
        "edited_arguments": getattr(approval, "edited_arguments", {}) or {},
        "resume_state": getattr(approval, "resume_state", {}) or {},
        "created_at": dt(approval.created_at),
        "resolved_at": dt(approval.resolved_at),
    }


def tool_run_to_dict(run) -> dict[str, Any]:
    return {
        "id": str(run.id),
        "turn_id": run.turn_id,
        "tool_name": run.tool_name,
        "status": run.status,
        "arguments": run.arguments or {},
        "result": run.result or {},
        "started_at": dt(run.started_at),
        "finished_at": dt(run.finished_at),
    }


def cron_job_to_dict(job) -> dict[str, Any]:
    return {
        "id": str(job.id),
        "name": job.name,
        "cron_expr": job.cron_expr,
        "timezone": job.timezone,
        "instruction": job.instruction,
        "enabled": job.enabled,
        "last_run_at": dt(job.last_run_at),
        "next_run_at": dt(job.next_run_at),
        "last_status": job.last_status,
        "last_result": job.last_result or {},
        "run_count": job.run_count,
        "failure_count": job.failure_count,
        "metadata": job.metadata_json or {},
        "created_at": dt(job.created_at),
        "updated_at": dt(job.updated_at),
    }


def mcp_server_to_dict(server) -> dict[str, Any]:
    return {
        "id": str(server.id),
        "name": server.name,
        "transport": server.transport,
        "command": server.command,
        "url": server.url,
        "config": server.config or {},
        "config_source": getattr(server, "config_source", "manual"),
        "permission_policy": getattr(server, "permission_policy", {}) or {},
        "session_mode": getattr(server, "session_mode", "transient"),
        "health_status": getattr(server, "health_status", "unknown"),
        "last_imported_checksum": getattr(server, "last_imported_checksum", "") or "",
        "enabled": server.enabled,
        "status": server.status,
        "last_connected_at": dt(server.last_connected_at),
        "last_error": server.last_error,
        "tool_count": server.tool_count,
        "tools_cache": server.tools_cache or [],
        "created_at": dt(server.created_at),
        "updated_at": dt(server.updated_at),
    }


def gateway_to_dict(gateway) -> dict[str, Any]:
    return {
        "id": str(gateway.id),
        "name": gateway.name,
        "kind": gateway.kind,
        "status": gateway.status,
        "endpoint": gateway.endpoint,
        "config": gateway.config or {},
        "enabled": gateway.enabled,
        "last_inbound_at": dt(gateway.last_inbound_at),
        "last_outbound_at": dt(gateway.last_outbound_at),
        "last_heartbeat_at": dt(getattr(gateway, "last_heartbeat_at", None)),
        "instance_id": getattr(gateway, "instance_id", ""),
        "version": getattr(gateway, "version", ""),
        "capabilities": getattr(gateway, "capabilities", []) or [],
        "last_error": gateway.last_error,
        "inbound_count": gateway.inbound_count,
        "outbound_count": gateway.outbound_count,
        "failure_count": gateway.failure_count,
        "created_at": dt(gateway.created_at),
        "updated_at": dt(gateway.updated_at),
    }


def a2a_connection_to_dict(connection) -> dict[str, Any]:
    return {
        "id": str(connection.id),
        "name": connection.name,
        "kind": connection.kind,
        "endpoint": connection.endpoint,
        "rpc_url": connection.rpc_url,
        "agent_card": connection.agent_card or {},
        "config": connection.config or {},
        "enabled": connection.enabled,
        "status": connection.status,
        "capabilities": connection.capabilities or [],
        "skills": connection.skills or [],
        "last_discovered_at": dt(connection.last_discovered_at),
        "last_error": connection.last_error,
        "created_at": dt(connection.created_at),
        "updated_at": dt(connection.updated_at),
    }


def a2a_task_to_dict(task, *, artifacts: list | None = None, events: list | None = None) -> dict[str, Any]:
    return {
        "id": str(task.id),
        "task_id": task.task_id,
        "connection_name": task.connection_name,
        "capability": task.capability,
        "context_id": task.context_id,
        "remote_task_id": task.remote_task_id,
        "remote_context_id": task.remote_context_id,
        "status": task.status,
        "input_text": task.input_text,
        "result": task.result or {},
        "error": task.error,
        "metadata": task.metadata_json or {},
        "started_at": dt(task.started_at),
        "finished_at": dt(task.finished_at),
        "created_at": dt(task.created_at),
        "updated_at": dt(task.updated_at),
        "artifacts": [a2a_artifact_to_dict(item) for item in artifacts] if artifacts is not None else None,
        "events": [a2a_event_to_dict(item) for item in events] if events is not None else None,
    }


def a2a_artifact_to_dict(artifact) -> dict[str, Any]:
    return {
        "id": str(artifact.id),
        "task_id": artifact.task_id,
        "artifact_id": artifact.artifact_id,
        "name": artifact.name,
        "mime_type": artifact.mime_type,
        "uri": artifact.uri,
        "content": artifact.content,
        "parts": artifact.parts or [],
        "metadata": artifact.metadata_json or {},
        "created_at": dt(artifact.created_at),
    }


def a2a_event_to_dict(event) -> dict[str, Any]:
    return {
        "id": str(event.id),
        "task_id": event.task_id,
        "event_type": event.event_type,
        "sequence": event.sequence,
        "payload": event.payload or {},
        "created_at": dt(event.created_at),
    }


def tool_definition_to_dict(tool) -> dict[str, Any]:
    return {
        "name": tool.name,
        "description": tool.description,
        "scope": tool.scope,
        "requires_approval": tool.requires_approval,
        "available": tool.available,
        "parameters": tool.parameters,
    }
