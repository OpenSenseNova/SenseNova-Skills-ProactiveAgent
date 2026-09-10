"""Harness-neutral outbound boundary for platform-specific Connectors."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Protocol, TextIO

from .bridge import BridgeHub
from .environment import set_compatible_env
from .contracts import (
    ConnectorId,
    OutboundEvent,
    SessionResumeFailed,
    SessionResumeRequested,
    SuggestionReady,
    TurnCompleted,
)


class ConnectorError(RuntimeError):
    """Base error for Connector registration and dispatch."""


class ConnectorConflictError(ConnectorError):
    """Raised when two Connectors claim the same identifier."""


class ConnectorNotFoundError(ConnectorError):
    """Raised when no Connector can handle an outbound event."""


class Connector(Protocol):
    """Own Harness-specific suggestion UI and visible Session resume behavior."""

    connector_id: ConnectorId

    def show_suggestion(self, event: SuggestionReady) -> None:
        """Display one suggestion and make approve/ignore available."""

    def resume_session(self, event: SessionResumeRequested) -> None:
        """Visibly continue the target Session with the authorized action."""


class ConnectorRouter:
    """Dispatch Core outbound events without branching on Harness type."""

    def __init__(self, connectors: Iterable[Connector] = ()) -> None:
        self._connectors: dict[ConnectorId, Connector] = {}
        for connector in connectors:
            self.register(connector)

    def register(self, connector: Connector) -> None:
        connector_id = connector.connector_id
        if not isinstance(connector_id, str) or not connector_id.strip():
            raise ValueError("connector_id must be a non-empty string")
        if connector_id in self._connectors:
            raise ConnectorConflictError(
                f"connector already registered: {connector_id}"
            )
        self._connectors[connector_id] = connector

    def dispatch(self, event: OutboundEvent) -> None:
        connector = self._connectors.get(event.platform)
        if connector is None:
            raise ConnectorNotFoundError(
                f"connector is not registered: {event.platform}"
            )

        if isinstance(event, SuggestionReady):
            connector.show_suggestion(event)
            return
        if isinstance(event, SessionResumeRequested):
            connector.resume_session(event)
            return
        raise TypeError(f"unsupported outbound event: {type(event).__name__}")


class HermesCliConnector:
    """Render CLI suggestions and visibly resume an authorized Hermes Session."""

    connector_id: ConnectorId = "hermes-cli"

    def __init__(
        self,
        executable: str | Path,
        *,
        service_url: str = "http://127.0.0.1:8080",
        output: TextIO | None = None,
        failure_sink: Callable[[SessionResumeFailed], None] | None = None,
        process_factory: Callable[..., subprocess.Popen[str]] = subprocess.Popen,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        self.executable = str(executable)
        self.service_url = service_url.rstrip("/")
        self.output = output or sys.stdout
        self.failure_sink = failure_sink
        self.process_factory = process_factory
        self.provider = provider
        self.model = model

    def _respond_prefix(self) -> str:
        """Return the command prefix used to submit a suggestion response.

        A checkout has no console-script entry point, so the response command
        must put the service package on ``PYTHONPATH`` and invoke its module.
        An installed wheel (including a ``pipx`` install), on the other hand,
        should use the generated ``sn-proactive-agent`` executable.  In
        particular, invoking ``python3`` against a pipx package can select a
        different interpreter and make the command fail even though the
        service is installed.

        The returned prefix includes the service module/console-script target;
        callers append ``respond <suggestion-id> ...`` to it.
        """
        source_root = Path(__file__).resolve().parents[1]
        # In the repository, ``service`` and ``connectors`` are sibling
        # directories under ``src``.  An installed wheel
        # contains only the package itself, so checking this sibling avoids
        # mistaking site-packages for a source checkout.
        if (source_root / "sn_proactive_agent_connectors").is_dir():
            return (
                f"PYTHONPATH={shlex.quote(str(source_root))} "
                "python3 -m sn_proactive_agent"
            )
        if shutil.which("sn-proactive-agent"):
            return "sn-proactive-agent"
        # When the service was launched with ``python -m`` or directly from a
        # console-script path that is not on PATH, stay inside the interpreter
        # that loaded this package.  A generic ``python3`` could resolve to a
        # different environment (especially from a pipx installation).
        return f"{shlex.quote(sys.executable)} -m sn_proactive_agent"

    def show_suggestion(self, event: SuggestionReady) -> None:
        prefix = self._respond_prefix()
        approve = (
            f"{prefix} respond "
            f"{shlex.quote(event.suggestion_id)} approve --url "
            f"{shlex.quote(self.service_url)}"
        )
        ignore = (
            f"{prefix} respond "
            f"{shlex.quote(event.suggestion_id)} ignore --url "
            f"{shlex.quote(self.service_url)}"
        )
        print("\n╭─ Proactive Agent 建议 ─────────────────────────────", file=self.output)
        print(f"│ {event.title}", file=self.output)
        print(f"│ 为什么现在：{event.why_now}", file=self.output)
        print(f"│ 依据：{event.evidence}", file=self.output)
        print(f"│ 建议动作：{event.suggested_action}", file=self.output)
        print("│", file=self.output)
        print(f"│ 同意：{approve}", file=self.output)
        print(f"│ 忽略：{ignore}", file=self.output)
        print("╰─────────────────────────────────────────────────────", file=self.output)
        self.output.flush()

    def resume_session(self, event: SessionResumeRequested) -> None:
        environment = os.environ.copy()
        set_compatible_env(environment, "SN_PROACTIVE_AGENT_SOURCE_SUGGESTION_ID", event.suggestion_id)
        environment["HERMES_ACCEPT_HOOKS"] = "1"
        command = [
            self.executable,
            "chat",
            "-q",
            event.suggested_action,
            "--resume",
            event.target_session_id,
            "--accept-hooks",
            "--cli",
            "--source",
            "cli",
        ]
        if self.provider:
            command.extend(("--provider", self.provider))
        if self.model:
            command.extend(("--model", self.model))
        print(
            f"\n[Proactive Agent] 已授权，正在可见续跑 Hermes Session "
            f"{event.target_session_id} …",
            file=self.output,
            flush=True,
        )
        try:
            process = self.process_factory(command, env=environment, text=True)
        except OSError as exc:
            self._report_failure(event.suggestion_id, f"Hermes 启动失败：{exc}")
            return
        threading.Thread(
            target=self._watch_process,
            args=(process, event.suggestion_id),
            name=f"hermes-resume-{event.suggestion_id}",
            daemon=True,
        ).start()

    def _watch_process(
        self,
        process: subprocess.Popen[str],
        suggestion_id: str,
    ) -> None:
        return_code = process.wait()
        if return_code == 0:
            print(
                f"[Proactive Agent] Hermes 续跑完成：{suggestion_id}",
                file=self.output,
                flush=True,
            )
            return
        self._report_failure(
            suggestion_id,
            f"Hermes 进程退出码为 {return_code}",
        )

    def _report_failure(self, suggestion_id: str, reason: str) -> None:
        print(
            f"[Proactive Agent] 续跑失败：{reason}",
            file=self.output,
            flush=True,
        )
        if self.failure_sink is not None:
            self.failure_sink(
                SessionResumeFailed(
                    suggestion_id=suggestion_id,
                    reason=reason,
                    failed_at=datetime.now(timezone.utc),
                )
            )


class HermesTuiConnector:
    """Deliver suggestions to an already-running Hermes TUI Session.

    This Connector never starts another Hermes process.  It publishes a
    session-scoped event to ``BridgeHub``; the TUI renders the interactive
    prompt and submits an authorized action through its existing gateway
    Session.
    """

    connector_id: ConnectorId = "hermes-tui"

    def __init__(
        self,
        hub: BridgeHub,
        *,
        log: Callable[[str], None] = print,
        show_suggestions: bool = True,
    ) -> None:
        self.hub = hub
        self.log = log
        self.show_suggestions = show_suggestions

    def show_suggestion(self, event: SuggestionReady) -> None:
        # The Web Dashboard reads the durable suggestion.ready record directly.
        # In Web-only mode the live TUI must not render a duplicate native
        # prompt, while resume_session remains enabled for an approved action.
        if not self.show_suggestions:
            self.log(
                f"[tui suggestion hidden] {event.suggestion_id} -> "
                f"{event.platform}/{event.target_session_id}; Web Dashboard owns display"
            )
            return
        self.hub.publish(
            "suggestion.ready",
            event.platform,
            event.target_session_id,
            event.to_payload(),
        )
        self.log(
            f"[tui suggestion] {event.suggestion_id} -> "
            f"{event.platform}/{event.target_session_id}"
        )

    def resume_session(self, event: SessionResumeRequested) -> None:
        self.hub.queue_resume_source(
            event.platform,
            event.target_session_id,
            event.suggestion_id,
        )
        self.hub.publish(
            "session.resume.requested",
            event.platform,
            event.target_session_id,
            event.to_payload(),
        )
        self.log(
            f"[tui resume] {event.suggestion_id} -> "
            f"{event.platform}/{event.target_session_id}"
        )


class HermesTuiSuggestionBridge:
    """Present another Connector's suggestion in the existing Hermes TUI.

    ACP remains the owner of the real Session and of an approved resume.  This
    adapter only creates a presentation copy for the TUI bridge.  The copy
    keeps the original ``suggestion_id`` so the TUI's existing
    ``suggestion.responded`` POST is resolved by Core back to the original ACP
    platform and Session.

    By default the TUI must be viewing the persisted Hermes Session whose ID is
    the ACP Session ID (for example ``hermes --tui --resume <acp-session-id>``).
    A mapper can be supplied when a host uses a different visible Session ID.
    """

    def __init__(
        self,
        hub: BridgeHub,
        *,
        ui_platform: ConnectorId = "hermes-tui",
        session_mapper: Callable[[SuggestionReady], str] | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        if not isinstance(ui_platform, str) or not ui_platform.strip():
            raise ValueError("ui_platform must be a non-empty string")
        self.hub = hub
        self.ui_platform = ui_platform.strip()
        self.session_mapper = session_mapper or (
            lambda event: event.target_session_id
        )
        self.log = log
        self._session_lock = threading.RLock()
        self._ui_sessions_by_suggestion: dict[str, str] = {}

    def __call__(self, event: SuggestionReady) -> None:
        ui_session_id = self.session_mapper(event)
        if not isinstance(ui_session_id, str) or not ui_session_id.strip():
            raise ValueError("mapped TUI session ID must be a non-empty string")
        ui_session_id = ui_session_id.strip()
        with self._session_lock:
            self._ui_sessions_by_suggestion[event.suggestion_id] = ui_session_id

        # This is a UI envelope, not a replacement V1 event.  Core's durable
        # suggestion remains keyed to the original ACP platform and Session.
        payload = event.to_payload()
        payload["platform"] = self.ui_platform
        payload["target_session_id"] = ui_session_id
        payload["_meta"] = {
            "source_platform": event.platform,
            "source_session_id": event.target_session_id,
        }
        self.hub.publish(
            "suggestion.ready",
            self.ui_platform,
            ui_session_id,
            payload,
        )
        self.log(
            f"[tui bridge suggestion] {event.suggestion_id}: "
            f"{event.platform}/{event.target_session_id} -> "
            f"{self.ui_platform}/{ui_session_id}"
        )

    def publish_resume_result(
        self,
        request: SessionResumeRequested,
        outcome: TurnCompleted | SessionResumeFailed,
    ) -> None:
        """Mirror an ACP resume result into the TUI's live Session stream."""

        with self._session_lock:
            ui_session_id = self._ui_sessions_by_suggestion.pop(
                request.suggestion_id,
                request.target_session_id,
            )

        if isinstance(outcome, TurnCompleted):
            payload = {
                "suggestion_id": request.suggestion_id,
                "platform": self.ui_platform,
                "target_session_id": ui_session_id,
                "suggested_action": request.suggested_action,
                "final_answer": outcome.final_answer,
                "completed_at": outcome.to_payload()["completed_at"],
                "source_platform": request.platform,
                "source_session_id": outcome.session_id,
                "source_turn_id": outcome.turn_id,
            }
            event_type = "session.resume.completed"
        elif isinstance(outcome, SessionResumeFailed):
            payload = {
                "suggestion_id": request.suggestion_id,
                "platform": self.ui_platform,
                "target_session_id": ui_session_id,
                "reason": outcome.reason,
                "failed_at": outcome.to_payload()["failed_at"],
                "source_platform": request.platform,
                "source_session_id": request.target_session_id,
            }
            event_type = "session.resume.failed"
        else:
            raise TypeError(f"unsupported ACP resume outcome: {type(outcome).__name__}")

        self.hub.publish(
            event_type,
            self.ui_platform,
            ui_session_id,
            payload,
        )
        self.log(
            f"[tui bridge {event_type}] {request.suggestion_id}: "
            f"{request.platform}/{request.target_session_id} -> "
            f"{self.ui_platform}/{ui_session_id}"
        )
