"""Harness-neutral outbound Connector for Codex App Server."""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from pathlib import Path

from proactive_memory_service.contracts import (
    SessionResumeFailed,
    SessionResumeRequested,
    SuggestionReady,
    TurnCompleted,
)

from .client import CodexAppServerClient


class CodexConnector:
    """Keep Codex suggestions in Web and resume the exact Codex Thread."""

    connector_id = "codex"

    def __init__(
        self,
        client: CodexAppServerClient,
        *,
        cwd: str | Path,
        suggestion_sink: Callable[[SuggestionReady], None] | None = None,
        resume_result_sink: Callable[
            [SessionResumeRequested, TurnCompleted | SessionResumeFailed],
            None,
        ]
        | None = None,
        asynchronous_resume: bool = True,
        log: Callable[[str], None] = print,
    ) -> None:
        self.client = client
        self.cwd = Path(cwd).expanduser().resolve()
        self.suggestion_sink = suggestion_sink or (lambda _event: None)
        self.resume_result_sink = resume_result_sink
        self.asynchronous_resume = asynchronous_resume
        self.log = log
        self._condition = threading.Condition(threading.RLock())
        self._active_resumes = 0

    def show_suggestion(self, event: SuggestionReady) -> None:
        # The Web dashboard is the visible suggestion surface for Codex.
        self.suggestion_sink(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        with self._condition:
            self._active_resumes += 1
        if not self.asynchronous_resume:
            self._run_resume(event)
            return
        worker = threading.Thread(
            target=self._run_resume,
            args=(event,),
            name=f"codex-resume-{event.suggestion_id}",
            daemon=True,
        )
        try:
            worker.start()
        except Exception:
            with self._condition:
                self._active_resumes -= 1
                self._condition.notify_all()
            raise

    def wait_until_idle(self, timeout_seconds: float | None = None) -> bool:
        with self._condition:
            if timeout_seconds is None:
                while self._active_resumes:
                    self._condition.wait()
                return True
            deadline = time.monotonic() + max(0.0, timeout_seconds)
            while self._active_resumes:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._condition.wait(timeout=remaining)
            return True

    def _run_resume(self, event: SessionResumeRequested) -> None:
        try:
            outcome = self.client.resume_authorized(event, self.cwd)
            if self.resume_result_sink is not None:
                self.resume_result_sink(event, outcome)
        except Exception as exc:
            self.log(
                f"[codex resume result delivery failed] "
                f"{event.suggestion_id}: {type(exc).__name__}: {exc}"
            )
        finally:
            with self._condition:
                self._active_resumes -= 1
                self._condition.notify_all()
