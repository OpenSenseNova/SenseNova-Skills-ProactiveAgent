from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from wsgiref.util import setup_testing_defaults

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from proactive_memory_connectors.codex import (  # noqa: E402
    CodexAppServerClient,
    CodexAppServerTransport,
    CodexConnector,
)
from proactive_memory_service.api import create_app  # noqa: E402
from proactive_memory_service.connector import ConnectorRouter  # noqa: E402
from proactive_memory_service.contracts import (  # noqa: E402
    SuggestionChoice,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
)
from proactive_memory_service.core import OrganizerContext, ProactiveMemoryCore  # noqa: E402
from proactive_memory_service.journal import RuntimeJournal  # noqa: E402
from proactive_memory_service.semantic import JudgeResult, OrganizationPlan  # noqa: E402
from proactive_memory_service.storage import (  # noqa: E402
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)


class E2EOrganizer:
    def organize(self, turn: TurnCompleted, context: OrganizerContext) -> OrganizationPlan:
        del turn, context
        project = ProjectMetadata(
            id="project-codex-e2e",
            name="Codex 适配验收",
            summary="验证 Codex App Server 到 Web 和状态存储的完整闭环。",
        )
        item = ItemState(
            id="item-full-lifecycle",
            name="完成 Codex 全链路",
            status="in_progress",
            goal="让每轮 Codex QA 可归档并支持建议触发的原 Thread 续跑。",
            completion_criteria="Project、Item、Event 和 runtime.jsonl 都可检查。",
            current_progress="已收到 Codex 完整回答。",
            next_step="等待建议决策。",
        )
        return OrganizationPlan(
            project=project,
            initial_item=item,
            event=OrganizedTurn(
                project_id=project.id,
                item_id=item.id,
                summary="Codex QA 已完成，状态进入统一存储。",
                updates=ItemUpdate(
                    {
                        "current_progress": "Codex QA 已写入 Project、Item 和 Event。",
                        "next_step": "等待用户处理 Web 建议。",
                    }
                ),
            ),
            reason="本轮属于 Codex 全链路验收事项。",
        )


class AlwaysSuggestJudge:
    def __init__(self) -> None:
        self.calls = 0

    def judge(self, *_args: object) -> JudgeResult:
        self.calls += 1
        return JudgeResult(
            True,
            "当前状态已经具备下一步行动条件。",
            "继续完成 Codex 验收",
            "Project/Item 已记录本轮完整 QA。",
            "继续处理 Codex 验收事项",
        )


def wsgi_request(app: Any, path: str, method: str = "GET", payload: dict[str, Any] | None = None) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    parsed = urlsplit(path)
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": parsed.path,
            "QUERY_STRING": parsed.query,
            "CONTENT_TYPE": "application/json" if payload is not None else "",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }
    )
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status

    raw = b"".join(app(environ, start_response))
    return int(captured["status"].split(" ", 1)[0]), json.loads(raw.decode("utf-8"))


class CodexCoreEndToEndTests(unittest.TestCase):
    def test_codex_to_web_to_same_thread_resume(self) -> None:
        command = [
            sys.executable,
            str(ROOT / "tests" / "fixtures" / "fake_codex_app_server.py"),
        ]
        with tempfile.TemporaryDirectory() as temporary_directory:
            data_root = Path(temporary_directory) / "data"
            store = MarkdownStore(data_root)
            journal = RuntimeJournal(data_root)
            judge = AlwaysSuggestJudge()
            suggestions: list[SuggestionReady] = []
            core_box: dict[str, ProactiveMemoryCore] = {}

            def resume_result_sink(_request: object, outcome: object) -> None:
                if isinstance(outcome, TurnCompleted):
                    core_box["core"].handle(outcome)

            with CodexAppServerTransport(command) as transport:
                client = CodexAppServerClient(transport, event_sink=lambda event: core_box["core"].handle(event))
                connector = CodexConnector(
                    client,
                    cwd=ROOT,
                    suggestion_sink=suggestions.append,
                    resume_result_sink=resume_result_sink,
                    asynchronous_resume=False,
                    log=lambda _message: None,
                )
                core = ProactiveMemoryCore(
                    store,
                    journal,
                    E2EOrganizer(),
                    judge,
                    ConnectorRouter((connector,)),
                    asynchronous=False,
                    log=lambda _message: None,
                )
                core_box["core"] = core
                app = create_app(core, store=store, journal=journal)
                client.initialize()
                thread = client.start_thread(ROOT)

                first = client.prompt(thread.thread_id, "请整理当前 Codex 适配验收进度")
                self.assertEqual(first.final_answer, "Codex 第一段，Codex 第二段。")
                status, dashboard = wsgi_request(app, "/api/dashboard")
                self.assertEqual(status, 200)
                self.assertEqual(dashboard["projects"][0]["id"], "project-codex-e2e")
                self.assertEqual(dashboard["projects"][0]["items"][0]["event_count"], 1)
                self.assertEqual(dashboard["suggestions"][0]["status"], "pending")
                self.assertEqual(len(suggestions), 1)

                suggestion_id = suggestions[0].suggestion_id
                status, accepted = wsgi_request(
                    app,
                    "/v1/events/suggestion.responded",
                    "POST",
                    SuggestionResponded(
                        suggestion_id=suggestion_id,
                        choice=SuggestionChoice.APPROVE,
                        responded_at=datetime.now(timezone.utc),
                    ).to_payload(),
                )
                self.assertEqual(status, 202)
                self.assertTrue(accepted["accepted"])

                status, dashboard = wsgi_request(app, "/api/dashboard")
                self.assertEqual(status, 200)
                self.assertEqual(dashboard["suggestions"][0]["status"], "completed")
                self.assertEqual(dashboard["projects"][0]["items"][0]["event_count"], 2)
                self.assertEqual(judge.calls, 1)
                self.assertEqual(len(journal.records("suggestion.executed")), 1)
                completed = journal.records("turn.completed")
                self.assertEqual(len(completed), 2)
                self.assertEqual(
                    completed[-1].payload["source_suggestion_id"],
                    suggestion_id,
                )
                self.assertEqual(
                    [record.kind for record in journal.records()],
                    [
                        "turn.started",
                        "turn.completed",
                        "organizer.result",
                        "judge.decision",
                        "suggestion.ready",
                        "daily_report.generated",
                        "suggestion.responded",
                        "session.resume.requested",
                        "turn.started",
                        "turn.completed",
                        "organizer.result",
                        "suggestion.executed",
                        "judge.decision",
                    ],
                )
                core.close()


if __name__ == "__main__":
    unittest.main()
