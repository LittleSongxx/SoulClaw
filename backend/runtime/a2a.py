"""A2A 1.0 orchestration runtime."""

from __future__ import annotations

import json
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import httpx
from sqlalchemy.orm import Session

from backend.domain.a2a import A2AService
from backend.infra.config import Settings
from backend.infra.events import RuntimeEventBus
from backend.infra.models import A2AAgentConnection, A2ATask

A2A_VERSION = "1.0"
TERMINAL_TASK_STATES = {"completed", "failed", "canceled", "rejected"}
PAUSED_TASK_STATES = {"input-required"}
A2A_STATE_BY_LOCAL_STATUS = {
    "submitted": "TASK_STATE_SUBMITTED",
    "working": "TASK_STATE_WORKING",
    "input-required": "TASK_STATE_INPUT_REQUIRED",
    "completed": "TASK_STATE_COMPLETED",
    "failed": "TASK_STATE_FAILED",
    "canceled": "TASK_STATE_CANCELED",
    "cancelled": "TASK_STATE_CANCELED",
    "rejected": "TASK_STATE_REJECTED",
    "auth-required": "TASK_STATE_AUTH_REQUIRED",
}
A2A_LOCAL_STATUS_BY_STATE = {
    "TASK_STATE_SUBMITTED": "submitted",
    "TASK_STATE_WORKING": "working",
    "TASK_STATE_INPUT_REQUIRED": "input-required",
    "TASK_STATE_COMPLETED": "completed",
    "TASK_STATE_FAILED": "failed",
    "TASK_STATE_CANCELED": "canceled",
    "TASK_STATE_CANCELLED": "canceled",
    "TASK_STATE_REJECTED": "rejected",
    "TASK_STATE_AUTH_REQUIRED": "auth-required",
}
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
            "supportedInterfaces": [
                {
                    "protocolBinding": "JSONRPC",
                    "protocolVersion": A2A_VERSION,
                    "url": f"{base_url}/api/a2a",
                }
            ],
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
                    "description": "Delegate evidence-driven research to a configured A2A 1.0 DeepResearch agent such as SoulSearcher.",
                    "tags": ["research", "deep-research", "soulsearcher"],
                    "inputModes": ["text/plain", "application/json"],
                    "outputModes": ["text/markdown", "application/json"],
                },
            ],
        }

    def discover(self, db: Session, connection_name: str) -> dict[str, Any]:
        connection = self._require_connection(db, connection_name)
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
        if connection is not None and task.remote_task_id:
            self._call_jsonrpc(connection, "CancelTask", {"id": task.remote_task_id})
        self.service.mark_task(db, task, status="canceled")
        return {"ok": True, "task": self.task_to_a2a(db, task)}

    def handle_jsonrpc(self, db: Session, payload: dict[str, Any]) -> dict[str, Any]:
        request_id = payload.get("id")
        method = str(payload.get("method") or "")
        params = payload.get("params") if isinstance(payload.get("params"), dict) else {}
        try:
            if method == "SendMessage":
                result = self._jsonrpc_message_send(db, params)
            elif method == "SendStreamingMessage":
                result = self._jsonrpc_message_send(db, params)
            elif method == "GetTask":
                task = self._task_from_params(db, params)
                result = self.task_to_a2a(db, task)
            elif method == "CancelTask":
                task = self._task_from_params(db, params)
                result = self.cancel_task(db, task.task_id)["task"]
            elif method == "SubscribeToTask":
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
            elif method == "ListTasks":
                result = {"tasks": [self.task_to_a2a(db, task) for task in self.service.list_tasks(db, limit=int(params.get("limit") or 100))]}
            elif method == "GetExtendedAgentCard":
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
        public_task_id = task.remote_task_id or task.task_id
        public_context_id = task.remote_context_id or task.context_id
        history = [
            self._message("user", task.input_text, context_id=public_context_id, task_id=public_task_id)
        ]
        if result.get("answer"):
            history.append(
                self._message("agent", str(result["answer"]), context_id=public_context_id, task_id=public_task_id)
            )
        return {
            "id": public_task_id,
            "contextId": public_context_id,
            "status": {
                "state": self._a2a_state(task.status),
                "timestamp": _now_iso(),
                "message": self._message("agent", task.error or result.get("answer", ""), context_id=public_context_id, task_id=public_task_id)
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
            ),
            "configuration": {
                "acceptedOutputModes": self._accepted_output_modes(connection),
            },
        }
        if self._supports_streaming(connection):
            try:
                return self._send_a2a_streaming_message(db, connection, task, params)
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code not in {400, 404, 405, 501}:
                    raise
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.streaming_fallback",
                    {"error": str(exc), "method": "SendStreamingMessage"},
                )
            except RuntimeError as exc:
                if not _looks_like_streaming_unsupported(str(exc)):
                    raise
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.streaming_fallback",
                    {"error": str(exc), "method": "SendStreamingMessage"},
                )
        response = self._call_jsonrpc(connection, "SendMessage", params)
        result = response.get("result") if isinstance(response, dict) else response
        if not isinstance(result, dict):
            result = {"raw": result}
        if "task" in result and isinstance(result.get("task"), dict):
            result = result["task"]
        elif "message" in result and isinstance(result.get("message"), dict):
            result = {"message": result["message"]}
        remote_task_id = str(result.get("id") or result.get("taskId") or "")
        remote_context_id = str(result.get("contextId") or "")
        status = self._status_from_a2a_task(result)
        answer = self._answer_from_a2a_result(result)
        for artifact in result.get("artifacts", []) if isinstance(result.get("artifacts"), list) else []:
            if isinstance(artifact, dict):
                self._persist_a2a_artifact(db, task, artifact)
        final_status = status if status in TERMINAL_TASK_STATES | PAUSED_TASK_STATES else "working"
        self.service.mark_task(
            db,
            task,
            status=final_status,
            result={"remote": result, "answer": answer},
            remote_task_id=remote_task_id,
            remote_context_id=remote_context_id,
        )
        return {
            "task_id": task.task_id,
            "remote_task_id": remote_task_id,
            "status": final_status,
            "answer": answer,
            "remote": result,
            "artifacts": [self._artifact_summary(item) for item in self.service.list_artifacts(db, task.task_id)],
        }

    def _send_a2a_streaming_message(
        self,
        db: Session,
        connection: A2AAgentConnection,
        task: A2ATask,
        params: dict[str, Any],
    ) -> dict[str, Any]:
        remote_task_id = ""
        remote_context_id = ""
        status = "working"
        answer = ""
        event_count = 0
        last_remote: dict[str, Any] = {}
        for result in self._call_jsonrpc_stream(connection, "SendStreamingMessage", params):
            event_count += 1
            last_remote = result
            if "task" in result and isinstance(result["task"], dict):
                remote_task_id, remote_context_id = self._remote_ids_from_result(
                    result["task"],
                    remote_task_id=remote_task_id,
                    remote_context_id=remote_context_id,
                )
                status = self._status_from_a2a_task(result["task"]) or status
                answer = self._answer_from_a2a_result(result["task"]) or answer
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.task",
                    {"remote": result["task"]},
                )
                self._persist_task_artifacts(db, task, result["task"])
            elif "statusUpdate" in result and isinstance(result["statusUpdate"], dict):
                update = result["statusUpdate"]
                remote_task_id, remote_context_id = self._remote_ids_from_result(
                    update,
                    remote_task_id=remote_task_id,
                    remote_context_id=remote_context_id,
                )
                status = self._status_from_status_update(update) or status
                answer = self._text_from_message(update.get("status", {}).get("message", {})) or answer
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.status_update",
                    {"remote": update},
                )
            elif "artifactUpdate" in result and isinstance(result["artifactUpdate"], dict):
                update = result["artifactUpdate"]
                remote_task_id, remote_context_id = self._remote_ids_from_result(
                    update,
                    remote_task_id=remote_task_id,
                    remote_context_id=remote_context_id,
                )
                artifact = update.get("artifact") if isinstance(update.get("artifact"), dict) else {}
                if artifact:
                    persisted = self._persist_a2a_artifact(db, task, artifact)
                    answer = persisted.content or answer
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.artifact_update",
                    {"remote": update},
                )
            elif "message" in result and isinstance(result["message"], dict):
                message = result["message"]
                remote_task_id, remote_context_id = self._remote_ids_from_result(
                    message,
                    remote_task_id=remote_task_id,
                    remote_context_id=remote_context_id,
                )
                answer = self._text_from_message(message) or answer
                self.service.add_event(
                    db,
                    task.task_id,
                    "a2a.message",
                    {"remote": message},
                )
            else:
                self.service.add_event(db, task.task_id, "a2a.event", {"remote": result})

            self.service.mark_task(
                db,
                task,
                status=status if status in TERMINAL_TASK_STATES | PAUSED_TASK_STATES else "working",
                remote_task_id=remote_task_id,
                remote_context_id=remote_context_id,
            )
            if status in TERMINAL_TASK_STATES | PAUSED_TASK_STATES:
                break

        final_status = status if status in TERMINAL_TASK_STATES | PAUSED_TASK_STATES else "working"
        self.service.mark_task(
            db,
            task,
            status=final_status,
            result={"remote": last_remote, "answer": answer, "events": event_count},
            remote_task_id=remote_task_id,
            remote_context_id=remote_context_id,
        )
        return {
            "task_id": task.task_id,
            "remote_task_id": remote_task_id,
            "status": final_status,
            "answer": answer,
            "events": event_count,
            "remote": last_remote,
            "artifacts": [self._artifact_summary(item) for item in self.service.list_artifacts(db, task.task_id)],
        }

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

    def _call_jsonrpc_stream(
        self,
        connection: A2AAgentConnection,
        method: str,
        params: dict[str, Any],
    ) -> Iterator[dict[str, Any]]:
        url = self._rpc_url(connection)
        payload = {"jsonrpc": "2.0", "id": str(uuid.uuid4()), "method": method, "params": params}
        headers = {
            "Accept": "text/event-stream",
            "Content-Type": "application/json",
            **self._headers(connection),
        }
        with httpx.Client(timeout=None) as client:
            with client.stream("POST", url, json=payload, headers=headers) as response:
                response.raise_for_status()
                for event in _iter_sse_events(response):
                    envelope = event.get("data") if isinstance(event.get("data"), dict) else event
                    if not isinstance(envelope, dict):
                        continue
                    if envelope.get("error"):
                        raise RuntimeError(json.dumps(envelope["error"], ensure_ascii=False))
                    result = envelope.get("result")
                    if isinstance(result, dict):
                        yield result

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
            "role": "ROLE_AGENT" if str(role).lower() == "agent" else "ROLE_USER",
            "parts": [{"text": text}],
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
            elif isinstance(part.get("data"), dict):
                texts.append(json.dumps(part["data"], ensure_ascii=False, default=str))
        return "\n".join(texts).strip()

    @staticmethod
    def _mime_from_parts(parts: Any) -> str:
        if not isinstance(parts, list):
            return "text/plain"
        for part in parts:
            if not isinstance(part, dict):
                continue
            if isinstance(part.get("mediaType"), str):
                return part["mediaType"]
            if isinstance(part.get("file"), dict) and isinstance(part["file"].get("mimeType"), str):
                return part["file"]["mimeType"]
        return "text/plain"

    @staticmethod
    def _a2a_state(status: str) -> str:
        normalized = str(status or "working").strip().lower().replace("_", "-")
        return A2A_STATE_BY_LOCAL_STATUS.get(normalized, str(status or "TASK_STATE_WORKING"))

    @staticmethod
    def _status_from_a2a_task(task: dict[str, Any]) -> str:
        status = task.get("status") if isinstance(task, dict) else {}
        state = status.get("state") if isinstance(status, dict) else ""
        return A2ARuntimeManager._local_status_from_a2a_state(state)

    @staticmethod
    def _status_from_status_update(update: dict[str, Any]) -> str:
        status = update.get("status") if isinstance(update, dict) else {}
        state = status.get("state") if isinstance(status, dict) else ""
        return A2ARuntimeManager._local_status_from_a2a_state(state)

    @staticmethod
    def _local_status_from_a2a_state(state: Any) -> str:
        raw = str(state or "").strip()
        if not raw:
            return "working"
        upper = raw.upper().replace("-", "_")
        if upper in A2A_LOCAL_STATUS_BY_STATE:
            return A2A_LOCAL_STATUS_BY_STATE[upper]
        lower = raw.lower().replace("_", "-")
        if lower.startswith("task-state-"):
            lower = lower.removeprefix("task-state-")
        return lower or "working"

    @staticmethod
    def _artifact_parts(artifact) -> list[dict[str, Any]]:
        if artifact.parts:
            return artifact.parts
        if artifact.content:
            return [{"text": artifact.content, "mediaType": artifact.mime_type or "text/plain"}]
        if artifact.uri:
            return [{"file": {"uri": artifact.uri, "mimeType": artifact.mime_type}}]
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
    def _accepted_output_modes(connection: A2AAgentConnection) -> list[str]:
        config = connection.config or {}
        configured = config.get("accepted_output_modes") or config.get("acceptedOutputModes")
        if isinstance(configured, list):
            values = [str(item).strip() for item in configured if str(item).strip()]
            if values:
                return values
        card = connection.agent_card or {}
        modes = card.get("defaultOutputModes")
        if isinstance(modes, list):
            values = [str(item).strip() for item in modes if str(item).strip()]
            if values:
                return values
        return ["text/markdown", "text/html", "application/json", "text/plain"]

    @staticmethod
    def _supports_streaming(connection: A2AAgentConnection) -> bool:
        card = connection.agent_card or {}
        capabilities = card.get("capabilities") if isinstance(card.get("capabilities"), dict) else {}
        if "streaming" in capabilities:
            return bool(capabilities.get("streaming"))
        return True

    def _persist_task_artifacts(self, db: Session, task: A2ATask, remote_task: dict[str, Any]) -> None:
        artifacts = remote_task.get("artifacts") if isinstance(remote_task, dict) else []
        for artifact in artifacts if isinstance(artifacts, list) else []:
            if isinstance(artifact, dict):
                self._persist_a2a_artifact(db, task, artifact)

    def _persist_a2a_artifact(self, db: Session, task: A2ATask, artifact: dict[str, Any]):
        parts = artifact.get("parts") if isinstance(artifact.get("parts"), list) else []
        content = self._text_from_parts(parts)
        return self.service.add_artifact(
            db,
            task.task_id,
            artifact_id=str(artifact.get("artifactId") or artifact.get("id") or ""),
            name=str(artifact.get("name") or "A2A Artifact"),
            mime_type=self._mime_from_parts(parts),
            content=content,
            parts=parts,
            metadata=artifact.get("metadata") if isinstance(artifact.get("metadata"), dict) else {},
        )

    @staticmethod
    def _remote_ids_from_result(
        result: dict[str, Any],
        *,
        remote_task_id: str = "",
        remote_context_id: str = "",
    ) -> tuple[str, str]:
        task_id = str(
            result.get("id")
            or result.get("taskId")
            or result.get("task_id")
            or remote_task_id
            or ""
        )
        context_id = str(
            result.get("contextId")
            or result.get("context_id")
            or remote_context_id
            or ""
        )
        return task_id, context_id

    @staticmethod
    def _answer_from_a2a_result(result: dict[str, Any]) -> str:
        if not isinstance(result, dict):
            return ""
        message = result.get("message")
        if isinstance(message, dict):
            text = A2ARuntimeManager._text_from_message(message)
            if text:
                return text
        status = result.get("status")
        if isinstance(status, dict) and isinstance(status.get("message"), dict):
            text = A2ARuntimeManager._text_from_message(status["message"])
            if text:
                return text
        artifacts = result.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in reversed(artifacts):
                if isinstance(artifact, dict):
                    text = A2ARuntimeManager._text_from_parts(artifact.get("parts"))
                    if text:
                        return text
        return ""

    @staticmethod
    def _agent_card_urls(connection: A2AAgentConnection) -> list[str]:
        base = (connection.endpoint or connection.rpc_url or "").rstrip("/")
        if not base:
            return []
        return [base] if base.endswith("/.well-known/agent-card.json") else [f"{base}/.well-known/agent-card.json"]

    @staticmethod
    def _rpc_url_from_card(card: dict[str, Any], *, fallback: str) -> str:
        for item in card.get("supportedInterfaces", []) if isinstance(card.get("supportedInterfaces"), list) else []:
            if not isinstance(item, dict) or not item.get("url"):
                continue
            binding = str(item.get("protocolBinding") or "").upper()
            version = str(item.get("protocolVersion") or "")
            if binding in {"JSONRPC", "JSON-RPC"} and version == A2A_VERSION:
                return str(item["url"])
        raise RuntimeError("Agent Card does not advertise an A2A 1.0 JSONRPC supported interface")

    @staticmethod
    def _headers(connection: A2AAgentConnection) -> dict[str, str]:
        config = connection.config or {}
        headers = {"A2A-Version": A2A_VERSION}
        headers.update(dict(config.get("headers") if isinstance(config.get("headers"), dict) else {}))
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
        return str(connection.rpc_url or connection.endpoint or "").rstrip("/")

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


def _looks_like_streaming_unsupported(message: str) -> bool:
    lowered = message.lower()
    return any(
        token in lowered
        for token in (
            "sendstreamingmessage",
            "streaming",
            "method not found",
            "not supported",
            "-32601",
        )
    )
