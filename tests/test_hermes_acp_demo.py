from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_ROOT = ROOT / "scripts"
if str(SCRIPTS_ROOT) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_ROOT))

from run_hermes_acp_demo import (  # noqa: E402
    BLOCKED_PROMPT,
    DEMO01_FIRST_PROMPT,
    DEMO01_SECOND_PROMPT,
    DEMO03_FIRST_PROMPT,
    DEMO03_SECOND_PROMPT,
    DEMO03_THIRD_PROMPT,
    ITEM_ID,
    PROJECT_ID,
    READY_PROMPT,
    SUGGESTED_ACTION,
    Demo01Judge,
    Demo01Organizer,
    Demo02Judge,
    Demo02Organizer,
    Demo03Judge,
    Demo03Organizer,
    tui_command,
)
from proactive_memory_service.connector import ConnectorRouter  # noqa: E402
from proactive_memory_service.contracts import (  # noqa: E402
    SessionResumeRequested,
    SuggestionChoice,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
)
from proactive_memory_service.core import ProactiveMemoryCore  # noqa: E402
from proactive_memory_service.journal import RuntimeJournal  # noqa: E402
from proactive_memory_service.storage import MarkdownStore  # noqa: E402


class RecordingConnector:
    connector_id = "hermes-acp"

    def __init__(self) -> None:
        self.suggestions: list[SuggestionReady] = []
        self.resumes: list[SessionResumeRequested] = []

    def show_suggestion(self, event: SuggestionReady) -> None:
        self.suggestions.append(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        self.resumes.append(event)


class HermesAcpDemoScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.connector = RecordingConnector()
        self.core = ProactiveMemoryCore(
            self.store,
            self.journal,
            Demo02Organizer(),
            Demo02Judge(),
            ConnectorRouter((self.connector,)),
            asynchronous=False,
            log=lambda _message: None,
        )

    @staticmethod
    def turn(
        turn_id: str,
        question: str,
        answer: str,
        *,
        source_suggestion_id: str | None = None,
    ) -> TurnCompleted:
        return TurnCompleted(
            platform="hermes-acp",
            session_id="session-demo-02",
            turn_id=turn_id,
            user_question=question,
            final_answer=answer,
            completed_at=datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc),
            source_suggestion_id=source_suggestion_id,
        )

    def test_demo_02_state_suggestion_and_source_result_close_once(self) -> None:
        self.core.handle(
            self.turn(
                "turn-blocked",
                BLOCKED_PROMPT,
                "当前因四项材料缺失而暂停。",
            )
        )
        blocked = self.store.read_item(PROJECT_ID, ITEM_ID)
        self.assertEqual(blocked.status, "blocked")
        self.assertIsNotNone(blocked.blocker)
        self.assertEqual(self.connector.suggestions, [])

        self.core.handle(
            self.turn(
                "turn-ready",
                READY_PROMPT,
                "星河科技_Logo_最终版_20260828\n"
                "星河科技_品牌色规范_最终版_20260828\n"
                "星河科技_产品主视觉_最终版_20260828\n"
                "星河科技_正式宣传文案_最终版_20260828",
            )
        )
        ready = self.store.read_item(PROJECT_ID, ITEM_ID)
        self.assertEqual(ready.status, "in_progress")
        self.assertIsNone(ready.blocker)
        self.assertEqual(len(self.connector.suggestions), 1)
        suggestion = self.connector.suggestions[0]
        self.assertEqual(suggestion.title, "制定前端开工任务单并排期")

        self.core.handle(
            SuggestionResponded(
                suggestion_id=suggestion.suggestion_id,
                choice=SuggestionChoice.APPROVE,
                responded_at=datetime(2026, 8, 28, 12, 1, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(len(self.connector.resumes), 1)
        self.assertEqual(self.connector.resumes[0].target_session_id, "session-demo-02")

        self.core.handle(
            self.turn(
                "turn-resumed",
                SUGGESTED_ACTION,
                "已生成前端开工任务单和排期草案。",
                source_suggestion_id=suggestion.suggestion_id,
            )
        )
        final_item = self.store.read_item(PROJECT_ID, ITEM_ID)
        self.assertEqual(
            final_item.next_step,
            "确认页面范围和负责人后开始前端制作。",
        )
        self.assertEqual(len(self.store.list_projects()), 1)
        events = self.store.read_events(PROJECT_ID, ITEM_ID)
        self.assertEqual(events.count("<!-- event:start "), 3)
        self.assertIn(BLOCKED_PROMPT, events)
        self.assertIn(READY_PROMPT, events)
        self.assertIn(SUGGESTED_ACTION, events)
        self.assertEqual(len(self.journal.records("suggestion.ready")), 1)
        self.assertEqual(len(self.journal.records("suggestion.executed")), 1)
        self.assertEqual(
            [record.payload["outcome"] for record in self.journal.records("judge.decision")],
            ["silent", "suggest", "source_suggestion_completed"],
        )

    def test_tui_command_routes_visible_submissions_through_acp(self) -> None:
        command = tui_command(
            "http://127.0.0.1:8094",
            Path("/opt/hermes"),
            "session-visible",
        )

        self.assertIn("PROACTIVE_MEMORY_ACP_SUBMIT=1", command)
        self.assertIn("PROACTIVE_MEMORY_TUI=1", command)
        self.assertIn("--resume session-visible", command)


class CrossSessionDemoScenarioTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.connector = RecordingConnector()

    def build_core(self, organizer: object, judge: object) -> ProactiveMemoryCore:
        return ProactiveMemoryCore(
            self.store,
            self.journal,
            organizer,
            judge,
            ConnectorRouter((self.connector,)),
            asynchronous=False,
            log=lambda _message: None,
        )

    @staticmethod
    def turn(
        session_id: str,
        turn_id: str,
        question: str,
        answer: str,
    ) -> TurnCompleted:
        return TurnCompleted(
            platform="hermes-acp",
            session_id=session_id,
            turn_id=turn_id,
            user_question=question,
            final_answer=answer,
            completed_at=datetime(2026, 8, 28, 13, 0, tzinfo=timezone.utc),
        )

    def test_demo_01_recalls_and_updates_one_item_across_sessions(self) -> None:
        core = self.build_core(Demo01Organizer(), Demo01Judge())
        core.handle(
            self.turn(
                "session-sdk-a",
                "turn-sdk-plan",
                DEMO01_FIRST_PROMPT,
                "目标和当前进展已经整理。",
            )
        )
        core.handle(
            self.turn(
                "session-sdk-b",
                "turn-sdk-follow-up",
                DEMO01_SECOND_PROMPT,
                "SDK 只等待运维确认发布窗口。",
            )
        )

        item = self.store.read_item(PROJECT_ID, ITEM_ID)
        self.assertEqual(len(self.store.list_projects()), 1)
        self.assertEqual(item.status, "blocked")
        self.assertEqual(item.next_step, "等待运维在明天下午确认发布窗口。")
        self.assertEqual(item.blocker, "运维尚未确认发布窗口；确认前不发布。")
        events = self.store.read_events(PROJECT_ID, ITEM_ID)
        self.assertEqual(events.count("<!-- event:start "), 2)
        self.assertIn(DEMO01_FIRST_PROMPT, events)
        self.assertIn(DEMO01_SECOND_PROMPT, events)
        self.assertEqual(self.connector.suggestions, [])
        self.assertEqual(
            [record.payload["outcome"] for record in self.journal.records("judge.decision")],
            ["silent", "silent"],
        )

    def test_demo_03_reopens_completed_item_and_suggests_once(self) -> None:
        core = self.build_core(Demo03Organizer(), Demo03Judge())
        core.handle(
            self.turn(
                "session-form-a",
                "turn-form-criteria",
                DEMO03_FIRST_PROMPT,
                "三条完成标准清楚且可验证。",
            )
        )
        core.handle(
            self.turn(
                "session-form-a",
                "turn-form-completed",
                DEMO03_SECOND_PROMPT,
                "已将表单标记为完成。",
            )
        )
        self.assertEqual(
            self.store.read_item(PROJECT_ID, ITEM_ID).status,
            "completed",
        )

        core.handle(
            self.turn(
                "session-form-b",
                "turn-form-correction",
                DEMO03_THIRD_PROMPT,
                "已记录移动端阻塞和影响。",
            )
        )
        item = self.store.read_item(PROJECT_ID, ITEM_ID)
        self.assertEqual(item.status, "in_progress")
        self.assertEqual(item.blocker, "手机端提交按钮被底部导航遮挡。")
        self.assertEqual(len(self.connector.suggestions), 1)
        suggestion = self.connector.suggestions[0]

        core.handle(
            SuggestionResponded(
                suggestion_id=suggestion.suggestion_id,
                choice=SuggestionChoice.IGNORE,
                responded_at=datetime(2026, 8, 28, 13, 1, tzinfo=timezone.utc),
            )
        )
        self.assertEqual(self.connector.resumes, [])
        self.assertEqual(len(self.journal.records("suggestion.responded")), 1)
        self.assertEqual(self.journal.records("suggestion.responded")[0].payload["choice"], "ignore")
        self.assertEqual(self.journal.records("session.resume.requested"), ())
        self.assertEqual(
            [record.payload["outcome"] for record in self.journal.records("judge.decision")],
            ["silent", "silent", "suggest"],
        )
        events = self.store.read_events(PROJECT_ID, ITEM_ID)
        self.assertEqual(events.count("<!-- event:start "), 3)
        self.assertIn(DEMO03_SECOND_PROMPT, events)
        self.assertIn(DEMO03_THIRD_PROMPT, events)

if __name__ == "__main__":
    unittest.main()
