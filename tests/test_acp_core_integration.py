from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults


ROOT = Path(__file__).resolve().parents[1]
CONNECTORS_ROOT = ROOT / "src" / "proactive_memory_connectors"
if str(CONNECTORS_ROOT) not in sys.path:
    sys.path.insert(0, str(CONNECTORS_ROOT))

from acp import (  # noqa: E402
    AcpClient,
    AcpConnector,
    AcpTurnIncomplete,
    StdioJsonRpcTransport,
    V1HttpEventSink,
)
from proactive_memory_service.api import create_app  # noqa: E402
from proactive_memory_service.connector import ConnectorRouter  # noqa: E402
from proactive_memory_service.contracts import (  # noqa: E402
    SessionResumeRequested,
    SuggestionChoice,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
)
from proactive_memory_service.core import (  # noqa: E402
    OrganizerContext,
    ProactiveMemoryCore,
)
from proactive_memory_service.journal import RuntimeJournal  # noqa: E402
from proactive_memory_service.semantic import JudgeResult, OrganizationPlan  # noqa: E402
from proactive_memory_service.storage import (  # noqa: E402
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)


class FixedClock:
    def __init__(self) -> None:
        self.value = datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc)

    def __call__(self) -> datetime:
        current = self.value
        self.value += timedelta(seconds=1)
        return current


class FixedOrganizer:
    def __init__(self) -> None:
        self.calls = 0

    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> OrganizationPlan:
        del turn, context
        self.calls += 1
        project = ProjectMetadata(
            id="project-acp",
            name="ACP 接入",
            summary="通过 ACP 将完整 QA 接入主动记忆服务。",
        )
        item = ItemState(
            id="item-core-closure",
            name="完成 ACP 到 Core 闭环",
            status="in_progress",
            goal="让 ACP 完整 QA 更新 Project、Item 和 Event。",
            completion_criteria="Markdown 状态与 runtime.jsonl 均留下可检查证据。",
            current_progress="ACP 已能汇总完整回答。",
            next_step="接入 Core 入站事件。",
        )
        return OrganizationPlan(
            project=project,
            initial_item=item,
            event=OrganizedTurn(
                project_id=project.id,
                item_id=item.id,
                summary="假 ACP Agent 的完整 QA 已进入 Core。",
                updates=ItemUpdate(
                    {
                        "current_progress": "ACP 完整 QA 已写入状态存储。",
                        "next_step": "验证 ACP Session 恢复。",
                    }
                ),
            ),
            reason="本轮 QA 明确属于 ACP 接入事项。",
        )


class CountingJudge:
    def __init__(self, result: JudgeResult) -> None:
        self.result = result
        self.calls = 0

    def judge(self, *_args: object) -> JudgeResult:
        self.calls += 1
        return self.result


class BlockingJudge(CountingJudge):
    def __init__(self, result: JudgeResult) -> None:
        super().__init__(result)
        self.entered = threading.Event()
        self.release = threading.Event()

    def judge(self, *_args: object) -> JudgeResult:
        self.calls += 1
        self.entered.set()
        if not self.release.wait(timeout=3):
            raise TimeoutError("test did not release Judge")
        return self.result


class RecordingConnector:
    connector_id = "acp-test"

    def __init__(self) -> None:
        self.suggestions: list[SuggestionReady] = []
        self.resumes: list[SessionResumeRequested] = []

    def show_suggestion(self, event: SuggestionReady) -> None:
        self.suggestions.append(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        self.resumes.append(event)


class _WsgiResponse:
    def __init__(self, body: bytes) -> None:
        self.body = body

    def __enter__(self) -> _WsgiResponse:
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self.body


class WsgiOpener:
    """Route urllib Requests through the real WSGI API without a TCP socket."""

    def __init__(self, app: Any) -> None:
        self.app = app
        self.paths: list[str] = []

    def __call__(self, request: Any, *, timeout: float) -> _WsgiResponse:
        del timeout
        parsed = urlsplit(request.full_url)
        self.paths.append(parsed.path)
        body = request.data or b""
        environ: dict[str, Any] = {}
        setup_testing_defaults(environ)
        environ.update(
            {
                "REQUEST_METHOD": request.get_method(),
                "PATH_INFO": parsed.path,
                "QUERY_STRING": parsed.query,
                "CONTENT_TYPE": request.headers.get("Content-type", ""),
                "CONTENT_LENGTH": str(len(body)),
                "wsgi.input": BytesIO(body),
            }
        )
        captured: dict[str, Any] = {}

        def start_response(status: str, headers: list[tuple[str, str]]) -> None:
            captured["status"] = status
            captured["headers"] = headers

        response_body = b"".join(self.app(environ, start_response))
        status_code = int(captured["status"].split(" ", 1)[0])
        if status_code >= 400:
            detail = json.loads(response_body.decode("utf-8"))
            raise RuntimeError(f"WSGI API returned {status_code}: {detail}")
        return _WsgiResponse(response_body)


class AcpCoreIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.organizer = FixedOrganizer()
        self.connector = RecordingConnector()
        self.agent_command = [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "fake_acp_agent.py"),
        ]

    def _client(
        self,
        core: ProactiveMemoryCore,
        transport: StdioJsonRpcTransport,
    ) -> tuple[AcpClient, WsgiOpener]:
        opener = WsgiOpener(create_app(core))
        sink = V1HttpEventSink(
            "http://proactive-memory.test",
            fail_open=False,
            opener=opener,
        )
        return (
            AcpClient(
                transport,
                platform="acp-test",
                event_sink=sink,
                clock=FixedClock(),
            ),
            opener,
        )

    def test_fake_acp_qa_updates_markdown_and_runtime_once(self) -> None:
        judge = CountingJudge(JudgeResult(False, "状态已更新，当前无需额外提醒。"))
        core = ProactiveMemoryCore(
            self.store,
            self.journal,
            self.organizer,
            judge,
            ConnectorRouter((self.connector,)),
            asynchronous=False,
            log=lambda _message: None,
        )

        with StdioJsonRpcTransport(self.agent_command) as transport:
            client, opener = self._client(core, transport)
            client.initialize()
            session = client.new_session(ROOT)
            completed = client.prompt(
                session.session_id,
                "把 ACP 完整 QA 接入 Core",
                turn_id="turn-acp-core-1",
            )

        project_file = self.data_root / "projects" / "project-acp" / "project.md"
        item_file = (
            self.data_root
            / "projects"
            / "project-acp"
            / "items"
            / "item-core-closure"
            / "item.md"
        )
        events_file = item_file.with_name("events.md")
        self.assertTrue(project_file.is_file())
        self.assertTrue(item_file.is_file())
        self.assertTrue(events_file.is_file())
        self.assertIn(
            "items/item-core-closure/item.md",
            project_file.read_text(encoding="utf-8"),
        )
        item = self.store.read_item("project-acp", "item-core-closure")
        self.assertEqual(item.current_progress, "ACP 完整 QA 已写入状态存储。")
        self.assertEqual(item.next_step, "验证 ACP Session 恢复。")
        events = events_file.read_text(encoding="utf-8")
        self.assertEqual(events.count("<!-- event:start "), 1)
        self.assertIn("把 ACP 完整 QA 接入 Core", events)
        self.assertIn("第一段回答，第二段回答。", events)
        self.assertIn("假 ACP Agent 的完整 QA 已进入 Core。", events)

        self.assertEqual(completed.final_answer, "第一段回答，第二段回答。")
        self.assertEqual(self.organizer.calls, 1)
        self.assertEqual(judge.calls, 1)
        self.assertTrue((self.data_root / "runtime.jsonl").is_file())
        self.assertEqual(
            [record.kind for record in self.journal.records()],
            [
                "turn.started",
                "turn.completed",
                "organizer.result",
                "judge.decision",
            ],
        )
        self.assertEqual(len(self.journal.records("turn.completed")), 1)
        decisions = self.journal.records("judge.decision")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].payload["outcome"], "silent")
        self.assertEqual(
            opener.paths,
            [
                "/v1/events/turn.started",
                "/v1/events/turn.completed",
            ],
        )

    def test_new_acp_turn_suppresses_old_suggestion_but_keeps_state(self) -> None:
        judge = BlockingJudge(
            JudgeResult(
                True,
                "旧状态原本值得提醒。",
                "继续 ACP 接入",
                "上一轮已经产生下一步。",
                "继续完成 ACP 接入。",
            )
        )
        core = ProactiveMemoryCore(
            self.store,
            self.journal,
            self.organizer,
            judge,
            ConnectorRouter((self.connector,)),
            asynchronous=True,
            log=lambda _message: None,
        )

        try:
            with StdioJsonRpcTransport(self.agent_command) as transport:
                client, _opener = self._client(core, transport)
                client.initialize()
                session = client.new_session(ROOT)
                client.prompt(
                    session.session_id,
                    "完成第一轮状态更新",
                    turn_id="turn-acp-old",
                )
                self.assertTrue(judge.entered.wait(timeout=2))

                with self.assertRaisesRegex(AcpTurnIncomplete, "cancelled"):
                    client.prompt(
                        session.session_id,
                        "__cancel__",
                        turn_id="turn-acp-new",
                    )

            judge.release.set()
            core.wait_until_idle()
        finally:
            judge.release.set()
            core.close()

        events = self.store.read_events("project-acp", "item-core-closure")
        self.assertEqual(events.count("<!-- event:start "), 1)
        self.assertIn("完成第一轮状态更新", events)
        self.assertEqual(judge.calls, 1)
        self.assertEqual(len(self.journal.records("turn.started")), 2)
        self.assertEqual(len(self.journal.records("turn.completed")), 1)
        decisions = self.journal.records("judge.decision")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(decisions[0].payload["outcome"], "discarded")
        self.assertIn("turn-acp-new", decisions[0].payload["reason"])
        self.assertEqual(self.journal.records("suggestion.ready"), ())
        self.assertEqual(self.connector.suggestions, [])

    def test_approval_resumes_same_acp_session_and_returns_source_id(self) -> None:
        judge = CountingJudge(
            JudgeResult(True, "来源建议执行结果不应再次进入 Judge。")
        )
        ui_suggestions: list[SuggestionReady] = []
        command = [*self.agent_command, "resume"]
        with StdioJsonRpcTransport(command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                clock=FixedClock(),
            )
            connector = AcpConnector(
                "acp-test",
                client,
                cwd=ROOT,
                suggestion_sink=ui_suggestions.append,
                asynchronous_resume=False,
            )
            core = ProactiveMemoryCore(
                self.store,
                self.journal,
                self.organizer,
                judge,
                ConnectorRouter((connector,)),
                asynchronous=False,
                log=lambda _message: None,
            )
            opener = WsgiOpener(create_app(core))
            sink = V1HttpEventSink(
                "http://proactive-memory.test",
                fail_open=False,
                opener=opener,
            )
            client.event_sink = sink
            client.initialize()

            suggestion = SuggestionReady(
                suggestion_id="suggestion-acp-resume",
                platform="acp-test",
                created_at=datetime(2026, 8, 28, 10, 0, tzinfo=timezone.utc),
                target_session_id="fake-session-1",
                title="继续完成 ACP 恢复",
                why_now="恢复逻辑尚待验收。",
                evidence="原 Session ID 已保存。",
                suggested_action="继续完成 ACP 原 Session 恢复测试",
            )
            self.journal.append(
                "suggestion.ready",
                suggestion.suggestion_id,
                suggestion.to_payload(),
                recorded_at=suggestion.created_at,
            )
            sink(
                SuggestionResponded(
                    suggestion_id=suggestion.suggestion_id,
                    choice=SuggestionChoice.APPROVE,
                    responded_at=datetime(2026, 8, 28, 10, 1, tzinfo=timezone.utc),
                )
            )

        self.assertEqual(
            client.sessions["fake-session-1"].restored_with,
            "session/resume",
        )
        self.assertEqual(len(self.journal.records("suggestion.responded")), 1)
        self.assertEqual(len(self.journal.records("session.resume.requested")), 1)
        completed_records = self.journal.records("turn.completed")
        self.assertEqual(len(completed_records), 1)
        completed_payload = completed_records[0].payload
        self.assertEqual(completed_payload["session_id"], "fake-session-1")
        self.assertEqual(
            completed_payload["user_question"],
            "继续完成 ACP 原 Session 恢复测试",
        )
        self.assertEqual(
            completed_payload["source_suggestion_id"],
            suggestion.suggestion_id,
        )
        self.assertEqual(len(self.journal.records("suggestion.executed")), 1)
        decisions = self.journal.records("judge.decision")
        self.assertEqual(len(decisions), 1)
        self.assertEqual(
            decisions[0].payload["outcome"],
            "source_suggestion_completed",
        )
        self.assertEqual(judge.calls, 0)
        self.assertEqual(self.journal.records("session.resume.failed"), ())
        self.assertEqual(
            opener.paths,
            [
                "/v1/events/suggestion.responded",
                "/v1/events/turn.started",
                "/v1/events/turn.completed",
            ],
        )

    def test_unsupported_acp_restore_is_recorded_without_creating_session(self) -> None:
        judge = CountingJudge(JudgeResult(True, "不应调用。"))
        with StdioJsonRpcTransport(self.agent_command) as transport:
            client = AcpClient(
                transport,
                platform="acp-test",
                clock=FixedClock(),
            )
            connector = AcpConnector(
                "acp-test",
                client,
                cwd=ROOT,
                suggestion_sink=lambda _event: None,
                asynchronous_resume=False,
            )
            core = ProactiveMemoryCore(
                self.store,
                self.journal,
                self.organizer,
                judge,
                ConnectorRouter((connector,)),
                asynchronous=False,
                log=lambda _message: None,
            )
            opener = WsgiOpener(create_app(core))
            sink = V1HttpEventSink(
                "http://proactive-memory.test",
                fail_open=False,
                opener=opener,
            )
            client.event_sink = sink
            client.initialize()

            suggestion = SuggestionReady(
                suggestion_id="suggestion-acp-unsupported",
                platform="acp-test",
                created_at=datetime(2026, 8, 28, 11, 0, tzinfo=timezone.utc),
                target_session_id="fake-session-1",
                title="尝试恢复",
                why_now="验证失败路径。",
                evidence="Agent 未声明恢复能力。",
                suggested_action="继续原 Session",
            )
            self.journal.append(
                "suggestion.ready",
                suggestion.suggestion_id,
                suggestion.to_payload(),
                recorded_at=suggestion.created_at,
            )
            sink(
                SuggestionResponded(
                    suggestion_id=suggestion.suggestion_id,
                    choice=SuggestionChoice.APPROVE,
                    responded_at=datetime(2026, 8, 28, 11, 1, tzinfo=timezone.utc),
                )
            )

        failures = self.journal.records("session.resume.failed")
        self.assertEqual(len(failures), 1)
        self.assertEqual(len(self.journal.records("suggestion.responded")), 1)
        self.assertEqual(len(self.journal.records("session.resume.requested")), 1)
        self.assertEqual(
            failures[0].payload["suggestion_id"],
            suggestion.suggestion_id,
        )
        self.assertIn("未声明", failures[0].payload["reason"])
        self.assertEqual(client.sessions, {})
        self.assertEqual(self.journal.records("turn.started"), ())
        self.assertEqual(self.journal.records("turn.completed"), ())
        self.assertEqual(self.organizer.calls, 0)
        self.assertEqual(judge.calls, 0)
        self.assertEqual(
            opener.paths,
            [
                "/v1/events/suggestion.responded",
                "/v1/events/session.resume.failed",
            ],
        )


if __name__ == "__main__":
    unittest.main()
