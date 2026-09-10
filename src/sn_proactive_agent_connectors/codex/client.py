"""Client for the Codex app-server thread/turn lifecycle."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any
from uuid import uuid4

from sn_proactive_agent.contracts import (
    InboundEvent,
    SessionResumeFailed,
    SessionResumeRequested,
    TurnCompleted,
    TurnStarted,
)

from .mapper import CodexMappingError, CodexTurnIncomplete, CodexV1Mapper
from .session import CodexInitialization, CodexThread, CodexTurnBuffer
from .transport import (
    CodexAppServerTransport,
    CodexRemoteError,
    CodexTransportError,
)


class CodexClientError(RuntimeError):
    """Base error for Codex app-server lifecycle failures."""


class CodexInitializationError(CodexClientError):
    """Raised when the app-server handshake fails."""


class CodexThreadNotFoundError(CodexClientError):
    """Raised when a turn targets an unknown thread."""


EventSink = Callable[[InboundEvent], None]
NotificationSink = Callable[[str, Mapping[str, Any]], None]


class CodexAppServerClient:
    """Drive initialize → thread/start/resume → turn/start for Codex."""

    def __init__(
        self,
        transport: CodexAppServerTransport,
        *,
        platform: str = "codex",
        event_sink: EventSink | None = None,
        notification_sink: NotificationSink | None = None,
        clock: Callable[[], Any] | None = None,
        client_name: str = "sn-proactive-agent",
        client_version: str = "0.1.1",
    ) -> None:
        self.transport = transport
        self.mapper = CodexV1Mapper(platform, clock=clock)
        self.event_sink = event_sink or (lambda _event: None)
        self.notification_sink = notification_sink or (lambda _method, _params: None)
        self.client_name = client_name
        self.client_version = client_version
        self.initialization: CodexInitialization | None = None
        self.threads: dict[str, CodexThread] = {}

    def initialize(self) -> CodexInitialization:
        if self.initialization is not None:
            return self.initialization
        result = self.transport.request(
            "initialize",
            {
                "clientInfo": {
                    "name": self.client_name,
                    "title": "Proactive Agent",
                    "version": self.client_version,
                },
                "capabilities": {},
            },
        )
        payload = _object(result, "initialize result")
        self.transport.notify("initialized", {})
        self.initialization = CodexInitialization(
            server_info=_optional_object(payload.get("serverInfo")),
            capabilities=dict(_optional_object(payload.get("capabilities")) or {}),
        )
        return self.initialization

    def start_thread(
        self,
        cwd: str | Path,
        *,
        model: str | None = None,
        approval_policy: str | None = "never",
        sandbox_policy: Mapping[str, Any] | None = None,
    ) -> CodexThread:
        self._require_initialized()
        normalized_cwd = Path(cwd).expanduser().resolve()
        params: dict[str, Any] = {"cwd": str(normalized_cwd)}
        if model:
            params["model"] = model
        if approval_policy:
            params["approvalPolicy"] = approval_policy
        if sandbox_policy is not None:
            params["sandboxPolicy"] = dict(sandbox_policy)
        payload = _object(self.transport.request("thread/start", params), "thread/start result")
        return self._remember_thread(payload, normalized_cwd)

    def resume_thread(self, thread_id: str, cwd: str | Path) -> CodexThread:
        self._require_initialized()
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CodexClientError("thread_id must be a non-empty string")
        normalized_cwd = Path(cwd).expanduser().resolve()
        payload = _object(
            self.transport.request(
                "thread/resume",
                {"threadId": thread_id},
            ),
            "thread/resume result",
        )
        thread = self._remember_thread(payload, normalized_cwd, fallback_id=thread_id)
        thread.restored = True
        return thread

    def prompt(
        self,
        thread_id: str,
        user_question: str,
        *,
        source_suggestion_id: str | None = None,
    ) -> TurnCompleted:
        thread = self.threads.get(thread_id)
        if thread is None:
            raise CodexThreadNotFoundError(f"unknown Codex thread: {thread_id}")
        if thread.active_turn is not None:
            raise CodexClientError(f"Codex thread already has an active turn: {thread_id}")

        buffer, started = self.mapper.begin_turn(
            thread_id=thread_id,
            turn_id=f"turn-{uuid4().hex}",
            user_question=user_question,
            source_suggestion_id=source_suggestion_id,
        )
        thread.active_turn = buffer
        started_sent = False
        terminal_status: str | None = None

        def on_notification(method: str, params: Mapping[str, Any]) -> None:
            nonlocal started_sent, terminal_status
            self.notification_sink(method, params)
            if method == "turn/started":
                raw_turn = params.get("turn")
                if isinstance(raw_turn, Mapping):
                    server_turn_id = raw_turn.get("id")
                    if isinstance(server_turn_id, str) and server_turn_id.strip():
                        buffer.turn_id = server_turn_id
                if not started_sent:
                    self.event_sink(
                        TurnStarted(
                            platform=self.mapper.platform,
                            session_id=thread_id,
                            turn_id=buffer.turn_id,
                            started_at=buffer.started_at,
                        )
                    )
                    started_sent = True
            if method == "turn/completed":
                raw_turn = params.get("turn")
                if isinstance(raw_turn, Mapping):
                    raw_status = raw_turn.get("status")
                    if isinstance(raw_status, str):
                        terminal_status = raw_status
                    # A client may opt out of item notifications.  In that
                    # case the completed turn still carries authoritative
                    # items, so use its final agent message as a fallback.
                    items = raw_turn.get("items")
                    if isinstance(items, list):
                        for item in items:
                            if (
                                isinstance(item, Mapping)
                                and item.get("type") == "agentMessage"
                                and isinstance(item.get("id"), str)
                                and isinstance(item.get("text"), str)
                            ):
                                buffer.set_message(item["id"], item["text"])
            self.mapper.apply_notification(buffer, method, params)

        def is_terminal(method: str, params: Mapping[str, Any]) -> bool:
            return method == "turn/completed" and params.get("threadId", thread_id) == thread_id

        try:
            payload = self.transport.request(
                "turn/start",
                {
                    "threadId": thread_id,
                    "input": [{"type": "text", "text": user_question}],
                },
                on_notification=on_notification,
                until_notification=is_terminal,
            )
            if not started_sent:
                self.event_sink(started)
            status = terminal_status or _turn_status(payload)
            completed = self.mapper.complete_turn(buffer, status=status)
            self.event_sink(completed)
            return completed
        except (CodexTransportError, CodexMappingError, CodexTurnIncomplete) as exc:
            raise CodexClientError(str(exc)) from exc
        finally:
            thread.active_turn = None

    def resume_authorized(
        self,
        request: SessionResumeRequested,
        cwd: str | Path,
    ) -> TurnCompleted | SessionResumeFailed:
        try:
            if request.platform != self.mapper.platform:
                raise CodexClientError("恢复请求的平台与 Codex Connector 不一致")
            self.resume_thread(request.target_session_id, cwd)
            return self.prompt(
                request.target_session_id,
                request.suggested_action,
                source_suggestion_id=request.suggestion_id,
            )
        except (CodexClientError, CodexRemoteError) as exc:
            failed = SessionResumeFailed(
                suggestion_id=request.suggestion_id,
                reason=f"Codex 原 Thread 续跑失败：{exc}",
                failed_at=self.mapper.timestamp(),
            )
            self.event_sink(failed)
            return failed

    def close(self) -> None:
        self.transport.close()

    def _require_initialized(self) -> None:
        if self.initialization is None:
            raise CodexInitializationError("initialize must complete first")

    def _remember_thread(
        self,
        payload: Mapping[str, Any],
        cwd: Path,
        *,
        fallback_id: str | None = None,
    ) -> CodexThread:
        raw_thread = payload.get("thread")
        thread_payload = raw_thread if isinstance(raw_thread, Mapping) else payload
        thread_id = thread_payload.get("id", fallback_id)
        if not isinstance(thread_id, str) or not thread_id.strip():
            raise CodexClientError("Codex thread result must contain thread.id")
        session_id = thread_payload.get("sessionId", thread_id)
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = thread_id
        thread = CodexThread(thread_id=thread_id, session_id=session_id, cwd=cwd)
        self.threads[thread_id] = thread
        return thread


def _object(value: Any, label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CodexClientError(f"{label} must be an object")
    return value


def _optional_object(value: Any) -> Mapping[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise CodexClientError("Codex metadata must be an object")
    return value


def _turn_status(payload: Any) -> str:
    result = _object(payload, "turn/start result")
    raw_turn = result.get("turn")
    turn = raw_turn if isinstance(raw_turn, Mapping) else result
    status = turn.get("status")
    if not isinstance(status, str) or not status:
        raise CodexClientError("Codex turn result must contain status")
    return status
