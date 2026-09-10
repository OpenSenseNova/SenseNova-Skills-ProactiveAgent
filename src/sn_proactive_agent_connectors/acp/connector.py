"""Core-facing Connector that composes ACP Session control with a UI sink."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from sn_proactive_agent.contracts import (
    SessionResumeFailed,
    SessionResumeRequested,
    SuggestionReady,
    TurnCompleted,
)

from .client import AcpClient


class AcpConnector:
    """Route suggestions to a UI and approved actions to the ACP Client."""

    def __init__(
        self,
        connector_id: str,
        client: AcpClient,
        *,
        cwd: str | Path,
        suggestion_sink: Callable[[SuggestionReady], None],
        mcp_servers: Sequence[Mapping[str, Any]] = (),
        asynchronous_resume: bool = True,
        resume_result_sink: Callable[
            [SessionResumeRequested, TurnCompleted | SessionResumeFailed],
            None,
        ]
        | None = None,
        log: Callable[[str], None] = print,
    ) -> None:
        if not isinstance(connector_id, str) or not connector_id.strip():
            raise ValueError("connector_id must be a non-empty string")
        if client.mapper.platform != connector_id:
            raise ValueError("connector_id must match the ACP Client platform")
        self.connector_id = connector_id
        self.client = client
        self.cwd = Path(cwd).expanduser().resolve()
        self.suggestion_sink = suggestion_sink
        self.mcp_servers = tuple(dict(server) for server in mcp_servers)
        self.asynchronous_resume = asynchronous_resume
        self.resume_result_sink = resume_result_sink
        self.log = log
        self._resume_condition = threading.Condition(threading.RLock())
        self._active_resumes = 0

    def show_suggestion(self, event: SuggestionReady) -> None:
        self.suggestion_sink(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        """Continue the ACP Session without blocking the TUI response POST."""

        with self._resume_condition:
            self._active_resumes += 1
        if not self.asynchronous_resume:
            self._run_resume(event)
            return

        worker = threading.Thread(
            target=self._run_resume,
            args=(event,),
            name=f"acp-resume-{event.suggestion_id}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            with self._resume_condition:
                self._active_resumes -= 1
                self._resume_condition.notify_all()
            raise

    def wait_until_idle(self, timeout_seconds: float | None = None) -> bool:
        """Wait for background resumes; intended for shutdown and acceptance."""

        if timeout_seconds is not None and timeout_seconds < 0:
            raise ValueError("timeout_seconds must be non-negative")
        deadline = (
            None if timeout_seconds is None else time.monotonic() + timeout_seconds
        )
        with self._resume_condition:
            while self._active_resumes:
                if deadline is None:
                    self._resume_condition.wait()
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._resume_condition.wait(timeout=remaining)
        return True

    def _run_resume(self, event: SessionResumeRequested) -> None:
        try:
            outcome = self.client.resume_authorized(
                event,
                self.cwd,
                mcp_servers=self.mcp_servers,
            )
            if self.resume_result_sink is not None:
                try:
                    self.resume_result_sink(event, outcome)
                except Exception as exc:
                    self.log(
                        f"[acp resume result delivery failed] "
                        f"{event.suggestion_id}: {type(exc).__name__}: {exc}"
                    )
        finally:
            with self._resume_condition:
                self._active_resumes -= 1
                self._resume_condition.notify_all()
