"""Small ACP v1 client that emits Proactive Agent turn events."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any
from uuid import uuid4

from sn_proactive_agent.contracts import (
    InboundEvent,
    SessionResumeFailed,
    SessionResumeRequested,
    TurnCompleted,
)

from .mapper import AcpMappingError, AcpV1Mapper, Clock
from .session import AcpInitialization, AcpSession
from .transport import AcpTransportError, StdioJsonRpcTransport


class AcpClientError(RuntimeError):
    """Base error for ACP lifecycle and session state failures."""


class AcpInitializationError(AcpClientError):
    """Raised when ACP negotiation does not produce a supported v1 result."""


class AcpSessionNotFoundError(AcpClientError):
    """Raised when a prompt targets a Session unknown to this client."""


class AcpSessionRestoreError(AcpClientError):
    """Raised when an existing ACP Session cannot be restored."""


class AcpSessionRestoreUnsupported(AcpSessionRestoreError):
    """Raised when an Agent advertises neither resume nor load support."""


EventSink = Callable[[InboundEvent], None]
AcpUpdateSink = Callable[[str, Mapping[str, Any]], None]


class AcpClient:
    """Drive initialize → session/new → session/prompt for one ACP Agent."""

    def __init__(
        self,
        transport: StdioJsonRpcTransport,
        *,
        platform: str,
        event_sink: EventSink | None = None,
        session_update_sink: AcpUpdateSink | None = None,
        clock: Clock | None = None,
        client_name: str = "sn-proactive-agent",
        client_version: str = "0.1.0",
    ) -> None:
        self.transport = transport
        self.mapper = AcpV1Mapper(platform, clock=clock)
        self.event_sink = event_sink or (lambda _event: None)
        self.session_update_sink = session_update_sink or (
            lambda _method, _params: None
        )
        self.client_name = client_name
        self.client_version = client_version
        self.initialization: AcpInitialization | None = None
        self.sessions: dict[str, AcpSession] = {}

    def initialize(
        self,
        *,
        client_capabilities: Mapping[str, Any] | None = None,
    ) -> AcpInitialization:
        if self.initialization is not None:
            return self.initialization
        result = self.transport.request(
            "initialize",
            {
                "protocolVersion": 1,
                "clientCapabilities": dict(client_capabilities or {}),
                "clientInfo": {
                    "name": self.client_name,
                    "title": "Proactive Agent",
                    "version": self.client_version,
                },
            },
        )
        payload = self._object(result, "initialize result")
        protocol_version = payload.get("protocolVersion")
        if isinstance(protocol_version, bool) or protocol_version != 1:
            raise AcpInitializationError(
                f"ACP protocol version is not supported: {protocol_version!r}"
            )
        raw_capabilities = payload.get("agentCapabilities", {})
        if not isinstance(raw_capabilities, Mapping):
            raise AcpInitializationError("agentCapabilities must be an object")
        raw_session_capabilities = raw_capabilities.get("sessionCapabilities")
        if raw_session_capabilities is not None and not isinstance(
            raw_session_capabilities,
            Mapping,
        ):
            raise AcpInitializationError("sessionCapabilities must be an object")
        raw_agent_info = payload.get("agentInfo")
        if raw_agent_info is not None and not isinstance(raw_agent_info, Mapping):
            raise AcpInitializationError("agentInfo must be an object when provided")

        self.initialization = AcpInitialization(
            protocol_version=protocol_version,
            agent_capabilities=dict(raw_capabilities),
            agent_info=None if raw_agent_info is None else dict(raw_agent_info),
        )
        return self.initialization

    def new_session(
        self,
        cwd: str | Path,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
    ) -> AcpSession:
        if self.initialization is None:
            raise AcpInitializationError("initialize must complete before session/new")
        normalized_cwd = Path(cwd).expanduser().resolve()
        result = self.transport.request(
            "session/new",
            {
                "cwd": str(normalized_cwd),
                "mcpServers": [dict(server) for server in mcp_servers],
            },
        )
        payload = self._object(result, "session/new result")
        session_id = payload.get("sessionId")
        if not isinstance(session_id, str) or not session_id.strip():
            raise AcpClientError("session/new result must contain sessionId")
        session = AcpSession(
            session_id=session_id,
            cwd=normalized_cwd,
            mcp_servers=tuple(dict(server) for server in mcp_servers),
        )
        self.sessions[session_id] = session
        return session

    def restore_session(
        self,
        session_id: str,
        cwd: str | Path,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
    ) -> AcpSession:
        """Reconnect to one exact Session without creating a replacement."""

        if self.initialization is None:
            raise AcpInitializationError(
                "initialize must complete before restoring a Session"
            )
        if not isinstance(session_id, str) or not session_id.strip():
            raise AcpSessionRestoreError("session_id must be a non-empty string")

        capabilities = self.initialization.agent_capabilities
        session_capabilities = capabilities.get("sessionCapabilities", {})
        supports_resume = (
            isinstance(session_capabilities, Mapping)
            and isinstance(session_capabilities.get("resume"), Mapping)
        )
        supports_load = capabilities.get("loadSession") is True
        if supports_resume:
            method = "session/resume"
        elif supports_load:
            method = "session/load"
        else:
            raise AcpSessionRestoreUnsupported(
                "Agent 未声明 sessionCapabilities.resume 或 loadSession"
            )

        normalized_cwd = Path(cwd).expanduser().resolve()
        copied_servers = tuple(dict(server) for server in mcp_servers)
        result = self.transport.request(
            method,
            {
                "sessionId": session_id,
                "cwd": str(normalized_cwd),
                "mcpServers": [dict(server) for server in copied_servers],
            },
            on_notification=lambda notification_method, params: (
                self._handle_restore_notification(
                    session_id,
                    notification_method,
                    params,
                )
            ),
        )
        if method == "session/resume" and not isinstance(result, Mapping):
            raise AcpSessionRestoreError(
                "session/resume result must be an object"
            )
        if method == "session/load" and result is not None and not isinstance(
            result,
            Mapping,
        ):
            raise AcpSessionRestoreError(
                "session/load result must be null or an object"
            )

        session = AcpSession(
            session_id=session_id,
            cwd=normalized_cwd,
            mcp_servers=copied_servers,
            restored_with=method,
        )
        self.sessions[session_id] = session
        return session

    def resume_authorized(
        self,
        request: SessionResumeRequested,
        cwd: str | Path,
        *,
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        turn_id: str | None = None,
    ) -> TurnCompleted | SessionResumeFailed:
        """Restore the requested Session and submit the authorized action."""

        try:
            if request.platform != self.mapper.platform:
                raise AcpSessionRestoreError(
                    "恢复请求的平台与 ACP Connector 不一致"
                )
            self.restore_session(
                request.target_session_id,
                cwd,
                mcp_servers=mcp_servers,
            )
            return self.prompt(
                request.target_session_id,
                request.suggested_action,
                turn_id=turn_id,
                source_suggestion_id=request.suggestion_id,
            )
        except (AcpClientError, AcpMappingError, AcpTransportError) as exc:
            failed = SessionResumeFailed(
                suggestion_id=request.suggestion_id,
                reason=f"ACP 原 Session 续跑失败：{exc}",
                failed_at=self.mapper.timestamp(),
            )
            self.event_sink(failed)
            return failed

    def prompt(
        self,
        session_id: str,
        user_question: str,
        *,
        turn_id: str | None = None,
        source_suggestion_id: str | None = None,
    ) -> TurnCompleted:
        session = self.sessions.get(session_id)
        if session is None:
            raise AcpSessionNotFoundError(f"unknown ACP Session: {session_id}")
        if session.active_turn is not None:
            raise AcpClientError(f"ACP Session already has an active turn: {session_id}")

        buffer, started = self.mapper.begin_turn(
            session_id=session_id,
            turn_id=turn_id or f"turn-{uuid4().hex}",
            user_question=user_question,
            source_suggestion_id=source_suggestion_id,
        )
        session.active_turn = buffer
        try:
            self.event_sink(started)
            result = self.transport.request(
                "session/prompt",
                {
                    "sessionId": session_id,
                    "prompt": [{"type": "text", "text": user_question}],
                },
                on_notification=lambda method, params: self._handle_notification(
                    buffer.session_id,
                    method,
                    params,
                ),
            )
            payload = self._object(result, "session/prompt result")
            stop_reason = payload.get("stopReason")
            if not isinstance(stop_reason, str) or not stop_reason:
                raise AcpClientError("session/prompt result must contain stopReason")
            completed = self.mapper.complete_turn(buffer, stop_reason=stop_reason)
            self.event_sink(completed)
            return completed
        finally:
            session.active_turn = None

    def _handle_notification(
        self,
        expected_session_id: str,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        if method != "session/update":
            return
        if params.get("sessionId") != expected_session_id:
            return
        self.session_update_sink(method, params)
        update = params.get("update")
        if not isinstance(update, Mapping):
            raise AcpClientError("session/update must contain an update object")
        session = self.sessions[expected_session_id]
        if session.active_turn is None:
            raise AcpClientError("received session/update without an active turn")
        self.mapper.apply_session_update(session.active_turn, update)

    def _handle_restore_notification(
        self,
        expected_session_id: str,
        method: str,
        params: Mapping[str, Any],
    ) -> None:
        # session/load replays history through session/update. It is UI history,
        # not a newly completed business Turn, so it must not emit V1 events.
        if method != "session/update":
            return
        session_id = params.get("sessionId")
        if session_id != expected_session_id:
            raise AcpSessionRestoreError(
                "Agent replayed a different Session during restore"
            )
        self.session_update_sink(method, params)

    @staticmethod
    def _object(value: Any, label: str) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise AcpClientError(f"{label} must be an object")
        return value
