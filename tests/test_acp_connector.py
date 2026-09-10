from __future__ import annotations

import sys
import unittest
from collections.abc import Mapping
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS_ROOT = ROOT / "src" / "sn_proactive_agent_connectors"
if str(CONNECTORS_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTORS_ROOT))

from acp import (  # noqa: E402
    AcpClient,
    AcpTurnIncomplete,
    StdioJsonRpcTransport,
    V1HttpEventSink,
)
from sn_proactive_agent.contracts import TurnCompleted, TurnStarted  # noqa: E402
from sn_proactive_agent.contracts import (  # noqa: E402
    SessionResumeFailed,
    SessionResumeRequested,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class AcpConnectorTests(unittest.TestCase):
    def setUp(self) -> None:
        self.agent_command = [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "fake_acp_agent.py"),
        ]

    def test_complete_acp_prompt_emits_one_complete_qa(self) -> None:
        events: list[TurnStarted | TurnCompleted] = []
        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )

            initialization = client.initialize()
            session = client.new_session(ROOT)
            completed = client.prompt(
                session.session_id,
                "请给我一个完整回答",
                turn_id="turn-acp-1",
            )

        self.assertEqual(initialization.protocol_version, 1)
        self.assertFalse(initialization.agent_capabilities["loadSession"])
        self.assertEqual(session.session_id, "fake-session-1")
        self.assertEqual(len(events), 2)
        self.assertIsInstance(events[0], TurnStarted)
        self.assertIsInstance(events[1], TurnCompleted)
        self.assertEqual(
            sum(isinstance(event, TurnCompleted) for event in events),
            1,
        )
        self.assertIs(completed, events[1])
        self.assertEqual(completed.platform, "acp-test")
        self.assertEqual(completed.session_id, "fake-session-1")
        self.assertEqual(completed.turn_id, "turn-acp-1")
        self.assertEqual(completed.user_question, "请给我一个完整回答")
        self.assertEqual(completed.final_answer, "第一段回答，第二段回答。")

    def test_non_successful_stop_does_not_emit_turn_completed(self) -> None:
        events: list[TurnStarted | TurnCompleted] = []
        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            session = client.new_session(ROOT)

            with self.assertRaisesRegex(AcpTurnIncomplete, "cancelled"):
                client.prompt(
                    session.session_id,
                    "__cancel__",
                    turn_id="turn-acp-cancelled",
                )

        self.assertEqual(len(events), 1)
        self.assertIsInstance(events[0], TurnStarted)
        self.assertFalse(any(isinstance(event, TurnCompleted) for event in events))

    def test_source_suggestion_id_survives_acp_mapping(self) -> None:
        events: list[TurnStarted | TurnCompleted] = []
        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            session = client.new_session(ROOT)
            completed = client.prompt(
                session.session_id,
                "执行已同意的建议",
                turn_id="turn-acp-suggestion",
                source_suggestion_id="suggestion-1",
            )

        self.assertEqual(completed.source_suggestion_id, "suggestion-1")

    def test_http_event_delivery_is_fail_open_by_default(self) -> None:
        messages: list[str] = []

        def unavailable(*_args: object, **_kwargs: object) -> object:
            raise OSError("service is unavailable")

        sink = V1HttpEventSink(
            "http://sn-proactive-agent.test",
            opener=unavailable,
            log=messages.append,
        )
        sink(
            TurnStarted(
                platform="acp-test",
                session_id="session-1",
                turn_id="turn-1",
                started_at=datetime(2026, 8, 28, tzinfo=timezone.utc),
            )
        )

        self.assertEqual(len(messages), 1)
        self.assertIn("failed to deliver turn.started", messages[0])

    def test_resume_capability_continues_the_exact_session(self) -> None:
        events: list[TurnStarted | TurnCompleted | SessionResumeFailed] = []
        command = [*self.agent_command, "resume"]
        with StdioJsonRpcTransport(command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            outcome = client.resume_authorized(
                SessionResumeRequested(
                    suggestion_id="suggestion-resume",
                    platform="acp-test",
                    target_session_id="fake-session-1",
                    suggested_action="继续完成 ACP 恢复测试",
                ),
                ROOT,
                turn_id="turn-resumed",
            )

        self.assertIsInstance(outcome, TurnCompleted)
        assert isinstance(outcome, TurnCompleted)
        self.assertEqual(outcome.session_id, "fake-session-1")
        self.assertEqual(outcome.user_question, "继续完成 ACP 恢复测试")
        self.assertEqual(outcome.final_answer, "已在原 Session 继续执行。")
        self.assertEqual(outcome.source_suggestion_id, "suggestion-resume")
        self.assertEqual(
            client.sessions["fake-session-1"].restored_with,
            "session/resume",
        )
        self.assertEqual([type(event) for event in events], [TurnStarted, TurnCompleted])

    def test_load_session_is_used_only_as_resume_fallback(self) -> None:
        events: list[TurnStarted | TurnCompleted | SessionResumeFailed] = []
        session_updates: list[Mapping[str, Any]] = []

        def record_update(_method: str, params: Mapping[str, Any]) -> None:
            session_updates.append(params)

        command = [*self.agent_command, "load"]
        with StdioJsonRpcTransport(command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                session_update_sink=record_update,
                clock=FixedClock(),
            )
            client.initialize()
            outcome = client.resume_authorized(
                SessionResumeRequested(
                    suggestion_id="suggestion-load",
                    platform="acp-test",
                    target_session_id="fake-session-1",
                    suggested_action="通过 load 继续原会话",
                ),
                ROOT,
                turn_id="turn-loaded",
            )

        self.assertIsInstance(outcome, TurnCompleted)
        assert isinstance(outcome, TurnCompleted)
        self.assertEqual(
            client.sessions["fake-session-1"].restored_with,
            "session/load",
        )
        self.assertEqual(outcome.final_answer, "已在原 Session 继续执行。")
        self.assertEqual(outcome.source_suggestion_id, "suggestion-load")
        # Replayed history from session/load is UI history, not a new V1 Turn.
        self.assertEqual([type(event) for event in events], [TurnStarted, TurnCompleted])
        self.assertEqual(
            [
                update["update"]["sessionUpdate"]
                for update in session_updates[:2]
            ],
            ["user_message_chunk", "agent_message_chunk"],
        )

    def test_missing_restore_capability_emits_failure_without_new_session(self) -> None:
        events: list[TurnStarted | TurnCompleted | SessionResumeFailed] = []
        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            outcome = client.resume_authorized(
                SessionResumeRequested(
                    suggestion_id="suggestion-unsupported",
                    platform="acp-test",
                    target_session_id="fake-session-1",
                    suggested_action="不能新建 Session 冒充恢复",
                ),
                ROOT,
            )

        self.assertIsInstance(outcome, SessionResumeFailed)
        assert isinstance(outcome, SessionResumeFailed)
        self.assertIn("未声明", outcome.reason)
        self.assertEqual(events, [outcome])
        self.assertEqual(client.sessions, {})

    def test_resume_rpc_error_emits_failure_without_retrying(self) -> None:
        events: list[TurnStarted | TurnCompleted | SessionResumeFailed] = []
        command = [*self.agent_command, "resume-error"]
        with StdioJsonRpcTransport(command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                event_sink=events.append,
                clock=FixedClock(),
            )
            client.initialize()
            outcome = client.resume_authorized(
                SessionResumeRequested(
                    suggestion_id="suggestion-resume-error",
                    platform="acp-test",
                    target_session_id="fake-session-1",
                    suggested_action="继续原 Session",
                ),
                ROOT,
            )

        self.assertIsInstance(outcome, SessionResumeFailed)
        assert isinstance(outcome, SessionResumeFailed)
        self.assertIn("session restore failed", outcome.reason)
        self.assertEqual(events, [outcome])
        self.assertEqual(client.sessions, {})


if __name__ == "__main__":
    unittest.main()
