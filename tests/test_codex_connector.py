from __future__ import annotations

import sys
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from proactive_memory_connectors.codex import (  # noqa: E402
    CodexAppServerClient,
    CodexAppServerTransport,
    CodexConnector,
)
from proactive_memory_service.contracts import (  # noqa: E402
    SessionResumeFailed,
    SessionResumeRequested,
    TurnCompleted,
    TurnStarted,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 9, 4, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class CodexConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.command = [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "fake_codex_app_server.py"),
        ]

    def test_codex_app_server_emits_one_complete_qa(self) -> None:
        events: list[object] = []
        with CodexAppServerTransport(self.command) as transport:
            client = CodexAppServerClient(
                transport,
                event_sink=events.append,
                clock=FixedClock(),
            )
            initialization = client.initialize()
            thread = client.start_thread(ROOT)
            completed = client.prompt(thread.thread_id, "总结这个项目")

        self.assertEqual(initialization.server_info["name"], "fake-codex")
        self.assertEqual(thread.thread_id, "thread-codex-1")
        self.assertEqual([type(event) for event in events], [TurnStarted, TurnCompleted])
        self.assertIsInstance(completed, TurnCompleted)
        self.assertEqual(completed.session_id, "thread-codex-1")
        self.assertEqual(completed.turn_id, "turn-codex-1")
        self.assertEqual(completed.final_answer, "Codex 第一段，Codex 第二段。")

    def test_resume_keeps_exact_thread_and_source_suggestion(self) -> None:
        events: list[object] = []
        with CodexAppServerTransport(self.command) as transport:
            client = CodexAppServerClient(
                transport,
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            outcome = client.resume_authorized(
                SessionResumeRequested(
                    suggestion_id="suggestion-codex-1",
                    platform="codex",
                    target_session_id="thread-codex-1",
                    suggested_action="继续整理这个项目",
                ),
                ROOT,
            )

        self.assertIsInstance(outcome, TurnCompleted)
        assert isinstance(outcome, TurnCompleted)
        self.assertEqual(outcome.session_id, "thread-codex-1")
        self.assertEqual(outcome.source_suggestion_id, "suggestion-codex-1")
        self.assertFalse(any(isinstance(event, SessionResumeFailed) for event in events))

    def test_connector_uses_web_sink_and_can_wait_for_resume(self) -> None:
        events: list[object] = []
        suggestions: list[object] = []
        with CodexAppServerTransport(self.command) as transport:
            client = CodexAppServerClient(transport, event_sink=events.append)
            client.initialize()
            client.start_thread(ROOT)
            connector = CodexConnector(
                client,
                cwd=ROOT,
                suggestion_sink=suggestions.append,
                asynchronous_resume=False,
            )
            connector.resume_session(
                SessionResumeRequested(
                    suggestion_id="suggestion-codex-2",
                    platform="codex",
                    target_session_id="thread-codex-1",
                    suggested_action="继续执行",
                )
            )
            self.assertTrue(connector.wait_until_idle(0))

        self.assertEqual(suggestions, [])
        self.assertTrue(any(isinstance(event, TurnCompleted) for event in events))

