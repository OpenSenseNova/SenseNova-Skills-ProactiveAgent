from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS_ROOT = ROOT / "src" / "sn_proactive_agent_connectors"
if str(CONNECTORS_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTORS_ROOT))

from acp import AcpClient, AcpConnector, StdioJsonRpcTransport  # noqa: E402
from sn_proactive_agent.api import create_app  # noqa: E402
from sn_proactive_agent.bridge import BridgeHub  # noqa: E402
from sn_proactive_agent.connector import (  # noqa: E402
    ConnectorRouter,
    HermesTuiSuggestionBridge,
)
from sn_proactive_agent.contracts import (  # noqa: E402
    SessionResumeRequested,
    SuggestionReady,
)
from sn_proactive_agent.core import (  # noqa: E402
    OrganizerContext,
    ProactiveAgentCore,
)
from sn_proactive_agent.journal import RuntimeJournal  # noqa: E402
from sn_proactive_agent.semantic import OrganizationPlan  # noqa: E402
from sn_proactive_agent.storage import (  # noqa: E402
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)


def request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    route, separator, query_string = path.partition("?")
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": route,
            "QUERY_STRING": query_string if separator else "",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }
    )
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return int(captured["status"].split(" ", 1)[0]), json.loads(response_body)


class SourceTurnOrganizer:
    def organize(
        self,
        turn: Any,
        context: OrganizerContext,
    ) -> OrganizationPlan:
        del turn, context
        project = ProjectMetadata(
            id="project-acp-tui",
            name="ACP 与 TUI 接线",
            summary="让 ACP 会话使用 Hermes TUI 的建议交互。",
        )
        item = ItemState(
            id="item-bridge",
            name="接通建议与响应",
            status="in_progress",
            goal="TUI 接受后由 ACP 恢复原 Session。",
            completion_criteria="结果带来源建议 ID 回流。",
            current_progress="建议已经显示。",
            next_step="验证接受响应。",
        )
        return OrganizationPlan(
            project=project,
            initial_item=item,
            event=OrganizedTurn(
                project_id=project.id,
                item_id=item.id,
                summary="ACP 已执行 TUI 中接受的建议。",
                updates=ItemUpdate(
                    {
                        "current_progress": "接受响应已恢复原 ACP Session。",
                        "next_step": "检查真实 TUI 展示。",
                    }
                ),
            ),
            reason="这是 ACP TUI Bridge 的来源建议回流。",
        )


class JudgeMustNotRun:
    def __init__(self) -> None:
        self.calls = 0

    def judge(self, *_args: object) -> Any:
        self.calls += 1
        raise AssertionError("来源建议回流不应再次调用 Judge")


class AcpTuiBridgeIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.bridge = BridgeHub()
        self.agent_command = [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "fake_acp_agent.py"),
            "resume",
        ]

    @staticmethod
    def suggestion() -> SuggestionReady:
        return SuggestionReady(
            suggestion_id="suggestion-acp-tui",
            platform="hermes-acp",
            created_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            target_session_id="fake-session-1",
            title="继续 ACP 接线",
            why_now="建议已经准备好等待用户确认。",
            evidence="ACP Session ID 已持久化。",
            suggested_action="继续完成 ACP 与 Hermes TUI 的接线",
        )

    def test_tui_approve_routes_back_to_original_acp_session(self) -> None:
        logs: list[str] = []
        tui_sink = HermesTuiSuggestionBridge(self.bridge, log=logs.append)
        judge = JudgeMustNotRun()

        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(transport, platform="hermes-acp")
            connector = AcpConnector(
                "hermes-acp",
                client,
                cwd=ROOT,
                suggestion_sink=tui_sink,
                resume_result_sink=tui_sink.publish_resume_result,
            )
            core = ProactiveAgentCore(
                self.store,
                self.journal,
                SourceTurnOrganizer(),
                judge,
                ConnectorRouter((connector,)),
                asynchronous=False,
                log=lambda _message: None,
            )
            client.event_sink = core.handle
            client.initialize()

            suggestion = self.suggestion()
            self.journal.append(
                "suggestion.ready",
                suggestion.suggestion_id,
                suggestion.to_payload(),
                recorded_at=suggestion.created_at,
            )
            connector.show_suggestion(suggestion)

            cursor, records = self.bridge.poll("hermes-tui", "fake-session-1")
            self.assertEqual(cursor, 1)
            self.assertEqual(len(records), 1)
            presented = records[0].payload
            self.assertEqual(records[0].event_type, "suggestion.ready")
            self.assertEqual(presented["suggestion_id"], suggestion.suggestion_id)
            self.assertEqual(presented["platform"], "hermes-tui")
            self.assertEqual(presented["target_session_id"], "fake-session-1")
            self.assertEqual(
                presented["_meta"],
                {
                    "source_platform": "hermes-acp",
                    "source_session_id": "fake-session-1",
                },
            )

            status, body = request(
                create_app(core, bridge=self.bridge),
                "POST",
                "/v1/events/suggestion.responded",
                {
                    "suggestion_id": suggestion.suggestion_id,
                    "choice": "approve",
                    "responded_at": "2026-08-28T12:01:00Z",
                },
            )
            self.assertEqual(status, 202)
            self.assertEqual(body["event_type"], "suggestion.responded")
            self.assertTrue(connector.wait_until_idle(timeout_seconds=3))

        self.assertEqual(
            client.sessions["fake-session-1"].restored_with,
            "session/resume",
        )
        resume_records = self.journal.records("session.resume.requested")
        self.assertEqual(len(resume_records), 1)
        self.assertEqual(resume_records[0].payload["platform"], "hermes-acp")
        self.assertEqual(
            resume_records[0].payload["target_session_id"],
            "fake-session-1",
        )
        completed = self.journal.records("turn.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(
            completed[0].payload["source_suggestion_id"],
            suggestion.suggestion_id,
        )
        self.assertEqual(len(self.journal.records("suggestion.executed")), 1)
        self.assertEqual(self.journal.records("session.resume.failed"), ())
        self.assertEqual(judge.calls, 0)

        # ACP owns the resume.  The TUI receives only the final display result,
        # not the old local ``session.resume.requested`` instruction, so it
        # cannot submit the action a second time.
        _cursor, final_bridge_records = self.bridge.poll(
            "hermes-tui", "fake-session-1"
        )
        self.assertEqual(
            [record.event_type for record in final_bridge_records],
            ["suggestion.ready", "session.resume.completed"],
        )
        resume_result = final_bridge_records[1].payload
        self.assertEqual(
            resume_result["suggested_action"],
            suggestion.suggested_action,
        )
        self.assertEqual(
            resume_result["final_answer"],
            "已在原 Session 继续执行。",
        )
        self.assertEqual(resume_result["source_session_id"], "fake-session-1")
        self.assertTrue(any("hermes-acp" in message for message in logs))

    def test_ignore_is_recorded_without_starting_acp_resume(self) -> None:
        class ClientThatMustNotResume:
            class Mapper:
                platform = "hermes-acp"

            mapper = Mapper()
            called = False

            def resume_authorized(self, *_args: object, **_kwargs: object) -> None:
                self.called = True
                raise AssertionError("ignore must not resume ACP")

        client = ClientThatMustNotResume()
        connector = AcpConnector(
            "hermes-acp",
            client,  # type: ignore[arg-type]
            cwd=ROOT,
            suggestion_sink=HermesTuiSuggestionBridge(
                self.bridge,
                log=lambda _message: None,
            ),
        )
        core = ProactiveAgentCore(
            self.store,
            self.journal,
            SourceTurnOrganizer(),
            JudgeMustNotRun(),
            ConnectorRouter((connector,)),
            asynchronous=False,
            log=lambda _message: None,
        )
        suggestion = self.suggestion()
        self.journal.append(
            "suggestion.ready",
            suggestion.suggestion_id,
            suggestion.to_payload(),
            recorded_at=suggestion.created_at,
        )

        status, _body = request(
            create_app(core, bridge=self.bridge),
            "POST",
            "/v1/events/suggestion.responded",
            {
                "suggestion_id": suggestion.suggestion_id,
                "choice": "ignore",
                "responded_at": "2026-08-28T12:01:00Z",
            },
        )

        self.assertEqual(status, 202)
        self.assertFalse(client.called)
        self.assertEqual(len(self.journal.records("suggestion.responded")), 1)
        self.assertEqual(self.journal.records("session.resume.requested"), ())

    def test_response_post_does_not_wait_for_a_slow_acp_resume(self) -> None:
        entered = threading.Event()
        release = threading.Event()

        class SlowClient:
            class Mapper:
                platform = "hermes-acp"

            mapper = Mapper()

            def resume_authorized(self, *_args: object, **_kwargs: object) -> None:
                entered.set()
                if not release.wait(timeout=2):
                    raise TimeoutError("test did not release ACP resume")

        connector = AcpConnector(
            "hermes-acp",
            SlowClient(),  # type: ignore[arg-type]
            cwd=ROOT,
            suggestion_sink=lambda _event: None,
        )
        core = ProactiveAgentCore(
            self.store,
            self.journal,
            SourceTurnOrganizer(),
            JudgeMustNotRun(),
            ConnectorRouter((connector,)),
            asynchronous=False,
            log=lambda _message: None,
        )
        suggestion = self.suggestion()
        self.journal.append(
            "suggestion.ready",
            suggestion.suggestion_id,
            suggestion.to_payload(),
            recorded_at=suggestion.created_at,
        )

        started_at = time.monotonic()
        status, _body = request(
            create_app(core, bridge=self.bridge),
            "POST",
            "/v1/events/suggestion.responded",
            {
                "suggestion_id": suggestion.suggestion_id,
                "choice": "approve",
                "responded_at": "2026-08-28T12:01:00Z",
            },
        )
        elapsed = time.monotonic() - started_at

        self.assertEqual(status, 202)
        self.assertTrue(entered.wait(timeout=1))
        self.assertLess(elapsed, 0.5)
        release.set()
        self.assertTrue(connector.wait_until_idle(timeout_seconds=1))


if __name__ == "__main__":
    unittest.main()
