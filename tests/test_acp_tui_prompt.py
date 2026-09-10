from __future__ import annotations

import io
import json
import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ROOT / "src"
CONNECTORS_ROOT = ROOT / "src" / "sn_proactive_agent_connectors"
for source_root in (SERVICE_ROOT, CONNECTORS_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from acp import AcpTuiPromptApplication  # noqa: E402
from sn_proactive_agent.api import create_app  # noqa: E402
from sn_proactive_agent.contracts import TurnCompleted  # noqa: E402


class RecordingPromptClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def prompt(self, session_id: str, user_question: str) -> TurnCompleted:
        self.calls.append((session_id, user_question))
        return TurnCompleted(
            platform="hermes-acp",
            session_id=session_id,
            turn_id=f"turn-visible-tui-{len(self.calls)}",
            user_question=user_question,
            final_answer="这是经 ACP 返回到 Hermes TUI 的回答。",
            completed_at=datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc),
        )


class AcpTuiPromptApplicationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.client = RecordingPromptClient()
        self.app = AcpTuiPromptApplication(
            create_app(),
            self.client,
            session_id="session-visible",
        )

    def request(
        self,
        path: str,
        *,
        method: str = "POST",
        payload: object | None = None,
        content_type: str = "application/json",
    ) -> tuple[int, dict[str, Any], list[tuple[str, str]]]:
        raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        environ = {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(raw)),
            "wsgi.input": io.BytesIO(raw),
        }
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = headers

        body = b"".join(self.app(environ, start_response))
        return (
            int(captured["status"].split(" ", 1)[0]),
            json.loads(body.decode("utf-8")),
            captured["headers"],
        )

    def test_visible_tui_prompt_is_submitted_to_exact_acp_session(self) -> None:
        status, body, _headers = self.request(
            AcpTuiPromptApplication.route,
            payload={
                "session_id": "session-visible",
                "user_question": "从 Hermes TUI 发送这个问题。",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(
            self.client.calls,
            [("session-visible", "从 Hermes TUI 发送这个问题。")],
        )
        self.assertEqual(body["session_id"], "session-visible")
        self.assertEqual(body["turn_id"], "turn-visible-tui-1")
        self.assertEqual(body["final_answer"], "这是经 ACP 返回到 Hermes TUI 的回答。")
        self.assertEqual(body["requested_session_id"], "session-visible")
        self.assertFalse(body["session_rebound"])

    def test_rejects_a_different_visible_session(self) -> None:
        status, body, _headers = self.request(
            AcpTuiPromptApplication.route,
            payload={
                "session_id": "session-other",
                "user_question": "不应发送。",
            },
        )

        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "session_mismatch"})
        self.assertEqual(self.client.calls, [])

    def test_visible_new_session_is_bounded_and_rebound_to_new_acp_session(self) -> None:
        created: list[str] = []

        def create_session() -> str:
            created.append("session-acp-second")
            return created[-1]

        app = AcpTuiPromptApplication(
            create_app(),
            self.client,
            session_id="session-visible",
            session_factory=create_session,
            max_sessions=2,
        )
        self.app = app

        status, body, _headers = self.request(
            AcpTuiPromptApplication.route,
            payload={
                "session_id": "hermes-new-visible-session",
                "user_question": "这是新 Session 的第一条消息。",
            },
        )

        self.assertEqual(status, 200)
        self.assertEqual(created, ["session-acp-second"])
        self.assertEqual(
            self.client.calls,
            [("session-acp-second", "这是新 Session 的第一条消息。")],
        )
        self.assertEqual(body["requested_session_id"], "hermes-new-visible-session")
        self.assertEqual(body["session_id"], "session-acp-second")
        self.assertTrue(body["session_rebound"])

        status, body, _headers = self.request(
            AcpTuiPromptApplication.route,
            payload={
                "session_id": "hermes-new-visible-session",
                "user_question": "同一新 Session 的第二条消息。",
            },
        )
        self.assertEqual(status, 200)
        self.assertEqual(created, ["session-acp-second"])
        self.assertEqual(body["session_id"], "session-acp-second")

        status, body, _headers = self.request(
            AcpTuiPromptApplication.route,
            payload={
                "session_id": "third-visible-session",
                "user_question": "不应创建第三个 ACP Session。",
            },
        )
        self.assertEqual(status, 409)
        self.assertEqual(body, {"error": "session_limit_reached"})

    def test_preserves_the_core_health_route(self) -> None:
        status, body, _headers = self.request("/health", method="GET")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})


if __name__ == "__main__":
    unittest.main()
