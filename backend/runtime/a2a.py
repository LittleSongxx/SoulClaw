"""A2A orchestration runtime and Weaver compatibility adapter."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import quote

import httpx
from sqlalchemy.orm import Session

from backend.domain.a2a import A2AService
from backend.infra.config import Settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import A2AAgentConnection, A2ATask

A2A_VERSION = "1.0"
TERMINAL_TASK_STATES = {"completed", "failed", "canceled", "rejected"}
PAUSED_TASK_STATES = {"input-required"}
HIGH_RISK_CAPABILITIES = {
    "code",
    "code-writing",
    "coding",
    "schedule",
    "calendar",
    "document-write",
    "write",
    "send",
    "external-write",
}


@dataclass(frozen=True)
class A2ADelegateRequest:
    capability: str
    query: str
    context: dict[str, Any] = field(default_factory=dict)
    files: list[dict[str, Any]] = field(default_factory=list)
    options: dict[str, Any] = field(default_factory=dict)
    connection_name: str = ""


class A2ARuntimeManager:
    def __init__(
        self,
        *,
        service: A2AService,
        events: RuntimeEventBus,
        settings: Settings,
        http_timeout_seconds: float = 60.0,
    ) -> None:
        self.service = service
        self.events = events
        self.settings = settings
        self.http_timeout_seconds = http_timeout_seconds

    def agent_card(self) -> dict[str, Any]:
        base_url = str(getattr(self.settings, "public_base_url", "") or "").rstrip("/")
        if not base_url:
            host = getattr(self.settings, "host", "127.0.0.1")
            port = getattr(self.settings, "port", 8020)
            base_url = f"http://{host}:{port}"
        return {
            "protocolVersion": A2A_VERSION,
            "name": "SoulClaw",
            "description": (
                "Local-first personal AI agent and A2A orchestrator for memory, Wiki, "
                "skills, tools, and delegated multi-agent work."
            ),
            "url": f"{base_url}/api/a2a",
            "preferredTransport": "JSONRPC",
            "supportedInterfaces": [{"transport": "JSONRPC", "url": f"{base_url}/api/a2a"}],
            "provider": {"organization": "SoulClaw", "url": base_url},
            "version": "2.0.0",
            "documentationUrl": f"{base_url}/",
            "capabilities": {"streaming": True, "pushNotifications": False, "extendedAgentCard": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/plain", "text/markdown", "application/json"],
            "skills": [
                {
                    "id": "orchestrate-agents",
                    "name": "Orchestrate Agents",
                    "description": "Route complex user tasks to configured A2A specialist agents.",
                    "tags": ["orchestration", "delegation", "multi-agent"],
                    "inputModes": ["text/plain", "application/json"],
                    "outputModes": ["application/json", "text/plain"],
                },
                {
                    "id": "deep-research",
                    "name": "Deep Research Delegation",
                    "description": "Delegate evidence-driven research to a configured DeepResearch agent such as Weaver.",
                    "tags": ["research", "deep-research", "weaver"],
                    "inputModes": ["text/plain", "application/json"],
                    "outputModes": ["text/markdown", "application/json"],
                },
            ],
        }

    def discover(self, db: Session, connection_name: str) -> dict[str, Any]:
        connection = self._require_connection(db, connection_name)
        if connection.kind == "weaver":
            card = self._weaver_agent_card(connection)
            self.service.update_discovery(db, connection, agent_card=card, status="online")
            return card
        urls = self._agent_card_urls(connection)
        last_error = ""
        with httpx.Client(timeout=self.http_timeout_seconds) as client:
            for url in urls:
                try:
                    response = client.get(url, headers=self._headers(connection))
                    response.raise_for_status()
                    card = response.json()
                    rpc_url = self._rpc_url_from_card(card, fallback=connection.rpc_url or connection.endpoint)
                    self.service.update_discovery(db, connection, agent_card=card, rpc_url=rpc_url, status="online")
                    return card
                except Exception as exc:  # noqa: BLE001
                    last_error = str(exc)
        self.service.update_discovery(db, connection, agent_card=connection.agent_card or {}, status="failed", error=last_error)
        raise RuntimeError(f"A2A discovery failed for {connection.name}: {last_error}")

    def delegate(self, db: Session, request: A2ADelegateRequest) -> dict[str, Any]:
        connection = self._select_connection(db, request)
        task = self.service.create_task(
            db,
            connection_name=connection.name,
            capability=request.capability,
            input_text=request.query,
            context_id=str(request.context.get("context_id") or request.options.get("context_id") or ""),
            metadata={"context": request.context, "files": request.files, "options": request.options},
        )
        self.service.mark_task(db, task, status="working")
        try:
            if connection.kind == "weaver":
                result = self._run_weaver(db, connection, task, request)
            else:
                result = self._send_a2a_message(db, connection, task, request)
        except Exception as exc:  # noqa: BLE001
            self.service.mark_task(db, task, status="failed", error=str(exc))
            raise
        return result

    def cancel_task(self, db: Session, task_id: str) -> dict[str, Any]:
        task = self.service.get_task_by_any_id(db, task_id)
        if task is None:
            raise KeyError(f"A2A task not found: {task_id}")
        if task.status in TERMINAL_TASK_STATES:
            return {"ok": True, "task": self.task_to_a2a(db, task)}
        connection = self.service.get_connection(db, task.connection_name)
        if connection is not None and connection.kind == "weaver" and task.remote_context_id:
            self._cancel_weaver(connection, task.remote_context_id)
        elif connection is not None and task.remote_task_id:
            self._call_jsonrpc(connection, "tasks/cancel", {"id": task.remote_task_id})
        self.service.mark_task(db, task, status="canceled")
        return {"ok": True, "task": self.task_to_a2a(db, task)}

    def handle_jsonrpc(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        try:
            if method == "message/send":
                result = self._jsonrpc_message_send(db, params)
            elif method == "message/stream":
                result = self._jsonrpc_message_send(db, params)
            elif method == "tasks/get":
                task = self._task_from_params(db, params)
                result = self.task_to_a2a(db, task)
            elif method == "tasks/cancel":
                task = self._task_from_params(db, params)
                result = self.cancel_task(db, task.task_id)["task"]
            elif method == "tasks/resubscribe":
                task = self._task_from_params(db, params)
                result = {
                    "task": self.task_to_a2a(db, task),
                    "events": [
                        {"type": item.event_type, "sequence": item.sequence, "data": item.payload or {}}
                        for item in self.service.list_events(
                            db,
                            task.task_id,
                            after_sequence=int(params.get("afterSequence") or params.get("after_sequence") or 0),
                        )
                    ],
                }
            elif method == "tasks/list":
                result = {"tasks": [self.task_to_a2a(db, task) for task in self.service.list_tasks(db, limit=int(params.get("limit") or 100))]}
            elif method == "agent/getAuthenticatedExtendedCard":
                result = self.agent_card()
            else:
                return self._jsonrpc_error(request_id, -32601, f"method not found: {method}")
            return {"jsonrpc": "2.0", "id": request_id, "result": result}
        except KeyError as exc:
            return self._jsonrpc_error(request_id, -32001, str(exc))
        except PermissionError as exc:
            return self._jsonrpc_error(request_id, -32003, str(exc))
        except Exception as exc:  # noqa: BLE001
            return self._jsonrpc_error(request_id, -32603, str(exc))

    def task_to_a2a(self, db: Session, task: A2ATask) -> dict[str, Any]:
        artifacts = self.service.list_artifacts(db, task.task_id)
        result = task.result or {}
        metadata = task.metadata_json or {}
        history = [
            self._message("user", task.input_text, context_id=task.context_id, task_id=task.remote_task_id or task.task_id)
        ]
        if result.get("answer"):
            history.append(
                self._message("agent", str(result["answer"]), context_id=task.context_id, task_id=task.remote_task_id or task.task_id)
            )
        return {
            "id": task.remote_task_id or task.task_id,
            "contextId": task.remote_context_id or task.context_id,
            "status": {
                "state": self._a2a_state(task.status),
                "timestamp": _now_iso(),
                "message": self._message("agent", task.error or result.get("answer", ""), context_id=task.context_id, task_id=task.remote_task_id or task.task_id)
                if task.error or result.get("answer")
                else None,
            },
            "artifacts": [
                {
                    "artifactId": item.artifact_id,
                    "name": item.name,
                    "parts": self._artifact_parts(item),
                    "metadata": item.metadata_json or {},
                }
                for item in artifacts
            ],
            "history": history,
            "metadata": {
                "localTaskId": task.task_id,
                "connectionName": task.connection_name,
                "capability": task.capability,
                **metadata,
            },
        }

    def _jsonrpc_message_send(self, db: Session, params: dict[str, Any]) -> dict[str, Any]:
        message = params.get("message") if isinstance(params.get("message"), dict) else params
        text = self._text_from_message(message)
        metadata = message.get("metadata") if isinstance(message, dict) and isinstance(message.get("metadata"), dict) else {}
        request = A2ADelegateRequest(
            capability=str(metadata.get("capability") or params.get("capability") or "deep-research"),
            query=text,
            context=metadata.get("context") if isinstance(metadata.get("context"), dict) else {},
            files=metadata.get("files") if isinstance(metadata.get("files"), list) else [],
            options=metadata.get("options") if isinstance(metadata.get("options"), dict) else {},
            connection_name=str(metadata.get("connection_name") or params.get("connection_name") or ""),
        )
        result = self.delegate(db, request)
        task = self.service.get_task(db, result["task_id"])
        if task is None:
            raise KeyError(f"A2A task not found: {result['task_id']}")
        return self.task_to_a2a(db, task)

    def _run_weaver(
        self,
        db: Session,
        connection: A2AAgentConnection,
        task: A2ATask,
        request: A2ADelegateRequest,
    ) -> dict[str, Any]:
        base_url = self._weaver_base_url(connection)
        payload = self._weaver_payload(connection, request)
        last_event: dict[str, Any] = {}
        answer = ""
        thread_id = ""
        event_count = 0
        with httpx.Client(timeout=None) as client:
            with client.stream(
                "POST",
                f"{base_url}/api/research/sse",
                headers={"Accept": "text/event-stream", "Content-Type": "application/json", **self._headers(connection)},
                json=payload,
            ) as response:
                response.raise_for_status()
                thread_id = response.headers.get("X-Thread-ID") or response.headers.get("x-thread-id") or ""
                if thread_id:
                    self.service.mark_task(db, task, status="working", remote_context_id=thread_id, remote_task_id=thread_id)
                for event in _iter_sse_events(response):
                    event_count += 1
                    last_event = event
                    mapped = self._map_weaver_event(event)
                    self.service.add_event(db, task.task_id, mapped["event_type"], {"source": "weaver", "event": event})
                    if mapped["status"]:
                        self.service.mark_task(db, task, status=mapped["status"], remote_context_id=thread_id, remote_task_id=thread_id)
                    extracted = self._extract_answer(event)
                    if extracted:
                        answer = extracted
                    artifact = self._artifact_from_weaver_event(event)
                    if artifact is not None:
                        self.service.add_artifact(db, task.task_id, **artifact)
                    if mapped["terminal"]:
                        break
        task = self.service.get_task(db, task.task_id) or task
        if task.status not in TERMINAL_TASK_STATES | PAUSED_TASK_STATES:
            self.service.mark_task(db, task, status="completed")
        self._collect_weaver_artifacts(db, connection, task, thread_id or task.remote_context_id, answer)
        result = {
            "task_id": task.task_id,
            "remote_task_id": thread_id or task.remote_task_id,
            "status": task.status,
            "answer": answer,
            "events": event_count,
            "last_event": last_event,
            "artifacts": [self._artifact_summary(item) for item in self.service.list_artifacts(db, task.task_id)],
        }
        self.service.mark_task(db, task, status="completed" if task.status not in {"failed", "canceled", "input-required"} else task.status, result=result)
        return result

    def _send_a2a_message(
        self,
        db: Session,
        connection: A2AAgentConnection,
        task: A2ATask,
        request: A2ADelegateRequest,
    ) -> dict[str, Any]:
        params = {
            "message": self._message(
                "user",
                request.query,
                context_id=task.context_id,
                metadata={
                    "capability": request.capability,
                    "context": request.context,
                    "files": request.files,
                    "options": request.options,
                },
            )
        }
        response = self._call_jsonrpc(connection, "message/send", params)
        result = response.get("result") if isinstance(response, dict) else response
        if not isinstance(result, dict):
            result = {"raw": result}
        remote_task_id = str(result.get("id") or result.get("taskId") or "")
        remote_context_id = str(result.get("contextId") or "")
        status = self._status_from_a2a_task(result)
        for artifact in result.get("artifacts", []) if isinstance(result.get("artifacts"), list) else []:
            if isinstance(artifact, dict):
                self.service.add_artifact(
                    db,
                    task.task_id,
                    artifact_id=str(artifact.get("artifactId") or ""),
                    name=str(artifact.get("name") or ""),
                    mime_type=self._mime_from_parts(artifact.get("parts")),
                    content=self._text_from_parts(artifact.get("parts")),
                    parts=artifact.get("parts") if isinstance(artifact.get("parts"), list) else [],
                    metadata=artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {},
                )
        final_status = status if status in TERMINAL_TASK_STATES | PAUSED_TASK_STATES else "working"
        self.service.mark_task(
            db,
            task,
            status=final_status,
            result={"remote": result},
            remote_task_id=remote_task_id,
            remote_context_id=remote_context_id,
        )
        return {
            "task_id": task.task_id,
            "remote_task_id": remote_task_id,
            "status": final_status,
            "remote": result,
            "artifacts": [self._artifact_summary(item) for item in self.service.list_artifacts(db, task.task_id)],
        }

    def _collect_weaver_artifacts(
        self,
        db: Session,
        connection: A2AAgentConnection,
        task: A2ATask,
        thread_id: str,
        answer: str,
    ) -> None:
        if answer:
            self.service.add_artifact(
                db,
                task.task_id,
                artifact_id="weaver-final-answer",
                name="Final Research Answer",
                mime_type="text/markdown",
                content=answer,
                metadata={"source": "weaver_stream"},
            )
        if not thread_id:
            return
        base_url = self._weaver_base_url(connection)
        with httpx.Client(timeout=self.http_timeout_seconds) as client:
            safe_thread = quote(thread_id, safe="")
            for path, artifact_id, name in [
                (f"/api/sessions/{safe_thread}/evidence", "weaver-evidence", "Research Evidence"),
                (f"/api/sessions/{safe_thread}", "weaver-session", "Research Session"),
            ]:
                try:
                    response = client.get(f"{base_url}{path}", headers=self._headers(connection))
                    if response.status_code >= 400:
                        continue
                    self.service.add_artifact(
                        db,
                        task.task_id,
                        artifact_id=artifact_id,
                        name=name,
                        mime_type="application/json",
                        content=json.dumps(response.json(), ensure_ascii=False, default=str),
                        metadata={"source": "weaver_api", "path": path},
                    )
                except Exception:  # noqa: BLE001
                    continue

    def _cancel_weaver(self, connection: A2AAgentConnection, thread_id: str) -> None:
        base_url = self._weaver_base_url(connection)
        safe_thread = quote(thread_id, safe="")
        with httpx.Client(timeout=self.http_timeout_seconds) as client:
            response = client.post(f"{base_url}/api/research/cancel/{safe_thread}", headers=self._headers(connection))
            if response.status_code not in {200, 202, 204, 404}:
                response.raise_for_status()

    def _call_jsonrpc(self, connection: A2AAgentConnection, method: str, params: dict[str, Any]) -> dict[str, Any]:
        url = self._rpc_url(connection)
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        with httpx.Client(timeout=self.http_timeout_seconds) as client:
            response = client.post(url, json=payload, headers={"Content-Type": "application/json", **self._headers(connection)})
            response.raise_for_status()
            data = response.json()
        if isinstance(data, dict) and data.get("error"):
            raise RuntimeError(json.dumps(data["error"], ensure_ascii=False))
        return data if isinstance(data, dict) else {"result": data}

    def _require_connection(self, db: Session, connection_name: str) -> A2AAgentConnection:
        connection = self.service.get_connection(db, connection_name)
        if connection is None:
            raise KeyError(f"A2A connection not found: {connection_name}")
        if not connection.enabled:
            raise RuntimeError(f"A2A connection disabled: {connection_name}")
        return connection

    def _select_connection(self, db: Session, request: A2ADelegateRequest) -> A2AAgentConnection:
        if request.connection_name:
            return self._require_connection(db, request.connection_name)
        capability = str(request.capability or "").strip().lower()
        candidates = self.service.list_connections(db, enabled=True, limit=500)
        for connection in candidates:
            tokens = {str(item).lower() for item in (connection.capabilities or [])}
            for skill in connection.skills or []:
                tokens.add(str(skill.get("id") or "").lower())
                tokens.add(str(skill.get("name") or "").lower())
                tokens.update(str(tag).lower() for tag in (skill.get("tags") or []))
            if capability and any(capability in token or token in capability for token in tokens if token):
                return connection
        for connection in candidates:
            if connection.kind == "weaver" and capability in {"deep-research", "research", "weaver"}:
                return connection
        raise KeyError(f"no enabled A2A connection matches capability: {request.capability}")

    @staticmethod
    def requires_approval(capability: str, options: dict[str, Any] | None = None) -> bool:
        text = str(capability or "").strip().lower()
        options = options or {}
        risk = str(options.get("risk") or options.get("risk_level") or "").strip().lower()
        return risk in {"high", "critical"} or any(token in text for token in HIGH_RISK_CAPABILITIES)

    @staticmethod
    def _message(role: str, text: str, *, context_id: str = "", task_id: str = "", metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        message = {
            "messageId": f"msg_{uuid.uuid4().hex}",
            "role": role,
            "parts": [{"kind": "text", "text": text}],
        }
        if context_id:
            message["contextId"] = context_id
        if task_id:
            message["taskId"] = task_id
        if metadata:
            message["metadata"] = metadata
        return message

    @staticmethod
    def _jsonrpc_error(request_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}}

    @staticmethod
    def _text_from_message(message: dict[str, Any]) -> str:
        if not isinstance(message, dict):
            return ""
        return A2ARuntimeManager._text_from_parts(message.get("parts"))

    @staticmethod
    def _text_from_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return ""
        texts: list[str] = []
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("text"), str):
                texts.append(part["text"])
            elif isinstance(part.get("textPart"), dict) and isinstance(part["textPart"].get("text"), str):
                texts.append(part["textPart"]["text"])
        return "\n".join(texts).strip()

    @staticmethod
    def _mime_from_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return "text/plain"
        for part in parts:
            if isinstance(part, dict) and isinstance(part.get("mimeType"), str):
                return part["mimeType"]
        return "text/plain"

    @staticmethod
    def _a2a_state(status: str) -> str:
        mapping = {
            "submitted": "submitted",
            "working": "working",
            "input-required": "input-required",
            "completed": "completed",
            "failed": "failed",
            "canceled": "canceled",
            "rejected": "rejected",
        }
        return mapping.get(status, status or "working")

    @staticmethod
    def _status_from_a2a_task(task: dict[str, Any]) -> str:
        status = task.get("status") if isinstance(task, dict) else {}
        state = str(status.get("state") if isinstance(status, dict) else "").strip().lower()
        if state.startswith("task_state_"):
            state = state.removeprefix("task_state_").replace("_", "-")
        return state or "working"

    @staticmethod
    def _artifact_parts(artifact) -> list[dict[str, Any]]:
        if artifact.parts:
            return artifact.parts
        if artifact.content:
            return [{"kind": "text", "text": artifact.content}]
        if artifact.uri:
            return [{"kind": "file", "file": {"uri": artifact.uri, "mimeType": artifact.mime_type}}]
        return []

    @staticmethod
    def _artifact_summary(artifact) -> dict[str, Any]:
        return {
            "artifact_id": artifact.artifact_id,
            "name": artifact.name,
            "mime_type": artifact.mime_type,
            "uri": artifact.uri,
            "content_length": len(artifact.content or ""),
        }

    @staticmethod
    def _agent_card_urls(connection: A2AAgentConnection) -> list[str]:
        base = (connection.endpoint or connection.rpc_url or "").rstrip("/")
        if not base:
            return []
        if base.endswith("/.well-known/agent-card.json"):
            return [base]
        return [f"{base}/.well-known/agent-card.json", f"{base}/.well-known/agent-card"]

    @staticmethod
    def _rpc_url_from_card(card: dict[str, Any], *, fallback: str) -> str:
        for item in card.get("supportedInterfaces", []) if isinstance(card.get("supportedInterfaces"), list) else []:
            if isinstance(item, dict) and str(item.get("transport") or "").upper() in {"JSONRPC", "JSON-RPC"} and item.get("url"):
                return str(item["url"])
        return str(card.get("url") or fallback or "").rstrip("/")

    @staticmethod
    def _weaver_base_url(connection: A2AAgentConnection) -> str:
        config = connection.config or {}
        return str(config.get("base_url") or connection.endpoint or "").rstrip("/")

    def _weaver_payload(self, connection: A2AAgentConnection, request: A2ADelegateRequest) -> dict[str, Any]:
        config = connection.config or {}
        deepsearch_config = dict(config.get("deepsearch_config") if isinstance(config.get("deepsearch_config"), dict) else {})
        deepsearch_config.update(request.options.get("deepsearch_config") if isinstance(request.options.get("deepsearch_config"), dict) else {})
        skill_ids = request.options.get("skill_ids") or config.get("skill_ids") or ["deep-research"]
        return {
            "query": request.query,
            "model": request.options.get("model") or config.get("model"),
            "user_id": request.context.get("user_id") or request.options.get("user_id") or config.get("user_id") or "soulclaw",
            "skill_ids": skill_ids if isinstance(skill_ids, list) else [str(skill_ids)],
            "deepsearch_config": deepsearch_config,
            "research_brief": request.context.get("research_brief") if isinstance(request.context.get("research_brief"), dict) else {},
        }

    @staticmethod
    def _weaver_agent_card(connection: A2AAgentConnection) -> dict[str, Any]:
        base_url = A2ARuntimeManager._weaver_base_url(connection)
        return {
            "protocolVersion": A2A_VERSION,
            "name": "Weaver DeepResearch",
            "description": "Evidence-driven DeepResearch agent exposed through SoulClaw's Weaver adapter.",
            "url": base_url,
            "preferredTransport": "SSE",
            "supportedInterfaces": [{"transport": "SSE", "url": f"{base_url}/api/research/sse"}],
            "provider": {"organization": "Weaver", "url": base_url},
            "version": "0.1.0",
            "capabilities": {"streaming": True, "pushNotifications": False},
            "defaultInputModes": ["text/plain", "application/json"],
            "defaultOutputModes": ["text/markdown", "application/json"],
            "skills": [
                {
                    "id": "deep-research",
                    "name": "Deep Research",
                    "description": "Conduct systematic multi-angle web research with evidence, report artifacts, and progress events.",
                    "tags": ["research", "deep-research", "weaver"],
                    "inputModes": ["text/plain", "application/json"],
                    "outputModes": ["text/markdown", "application/json"],
                }
            ],
        }

    @staticmethod
    def _map_weaver_event(event: dict[str, Any]) -> dict[str, Any]:
        event_type = str(event.get("type") or event.get("event") or "").strip()
        status = ""
        terminal = False
        if event_type == "interrupt":
            status = "input-required"
            terminal = True
        elif event_type in {"done", "completion", "report_written"}:
            status = "completed"
            terminal = True
        elif event_type in {"cancelled", "canceled"}:
            status = "canceled"
            terminal = True
        elif event_type == "error":
            status = "failed"
            terminal = True
        elif event_type:
            status = "working"
        return {"event_type": f"weaver.{event_type or 'event'}", "status": status, "terminal": terminal}

    @staticmethod
    def _extract_answer(event: dict[str, Any]) -> str:
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        for key in ("content", "text", "message", "answer", "report", "final_report"):
            value = data.get(key) if isinstance(data, dict) else None
            if isinstance(value, str) and value.strip():
                return value.strip()
        nested = data.get("data") if isinstance(data, dict) and isinstance(data.get("data"), dict) else {}
        for key in ("content", "text", "message", "answer", "report", "final_report"):
            value = nested.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return ""

    @staticmethod
    def _artifact_from_weaver_event(event: dict[str, Any]) -> dict[str, Any] | None:
        event_type = str(event.get("type") or event.get("event") or "").strip()
        if event_type not in {"artifact", "report_written", "completion"}:
            return None
        data = event.get("data") if isinstance(event.get("data"), dict) else event
        content = A2ARuntimeManager._extract_answer(event)
        name = str(data.get("name") or data.get("title") or "Weaver Artifact") if isinstance(data, dict) else "Weaver Artifact"
        mime_type = str(data.get("mime_type") or data.get("mimeType") or "text/markdown") if isinstance(data, dict) else "text/markdown"
        return {
            "artifact_id": str(data.get("artifact_id") or data.get("artifactId") or f"weaver-{event_type}") if isinstance(data, dict) else f"weaver-{event_type}",
            "name": name,
            "mime_type": mime_type,
            "content": content,
            "metadata": {"source_event_type": event_type, "raw": data if isinstance(data, dict) else {}},
        }

    @staticmethod
    def _headers(connection: A2AAgentConnection) -> dict[str, str]:
        config = connection.config or {}
        headers = dict(config.get("headers") if isinstance(config.get("headers"), dict) else {})
        internal_key = str(config.get("internal_api_key") or "")
        if internal_key:
            headers.setdefault("Authorization", f"Bearer {internal_key}")
            headers.setdefault("X-API-Key", internal_key)
        user_header = str(config.get("auth_user_header") or config.get("user_header") or "")
        user_id = str(config.get("user_id") or "")
        if user_header and user_id:
            headers.setdefault(user_header, user_id)
        return {str(key): str(value) for key, value in headers.items()}

    @staticmethod
    def _rpc_url(connection: A2AAgentConnection) -> str:
        return str(connection.rpc_url or connection.agent_card.get("url") or connection.endpoint or "").rstrip("/")

    def _task_from_params(self, db: Session, params: dict[str, Any]) -> A2ATask:
        task_id = str(params.get("id") or params.get("taskId") or params.get("task_id") or "")
        task = self.service.get_task_by_any_id(db, task_id)
        if task is None:
            raise KeyError(f"A2A task not found: {task_id}")
        return task


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _iter_sse_events(response: httpx.Response) -> Iterator[dict[str, Any]]:
    event_name = ""
    data_lines: list[str] = []
    for line in response.iter_lines():
        if line == "":
            if data_lines:
                payload = _parse_sse_payload(event_name, data_lines)
                if payload:
                    yield payload
            event_name = ""
            data_lines = []
            continue
        if line.startswith(":"):
            continue
        if line.startswith("event:"):
            event_name = line.split(":", 1)[1].strip()
        elif line.startswith("data:"):
            data_lines.append(line.split(":", 1)[1].lstrip())
    if data_lines:
        payload = _parse_sse_payload(event_name, data_lines)
        if payload:
            yield payload


def _parse_sse_payload(event_name: str, data_lines: list[str]) -> dict[str, Any] | None:
    raw = "\n".join(data_lines)
    try:
        parsed = json.loads(raw)
    except Exception:
        return {"type": event_name or "message", "data": {"text": raw}}
    if isinstance(parsed, dict) and "type" in parsed:
        return parsed
    return {"type": event_name or "message", "data": parsed if isinstance(parsed, dict) else {"value": parsed}}
