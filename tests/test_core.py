from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sn_proactive_agent.connector import ConnectorRouter
from sn_proactive_agent.contracts import (
    SessionResumeRequested,
    SuggestionChoice,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
    TurnStarted,
)
from sn_proactive_agent.core import OrganizerContext, ProactiveAgentCore
from sn_proactive_agent.journal import RuntimeJournal
from sn_proactive_agent.semantic import JudgeResult, OrganizationPlan
from sn_proactive_agent.storage import (
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)


NOW = datetime(2026, 8, 25, 3, 0, tzinfo=timezone.utc)


class FixedOrganizer:
    def organize(self, turn: TurnCompleted, context: OrganizerContext) -> OrganizationPlan:
        del turn, context
        project = ProjectMetadata(
            "project-001",
            "Proactive Agent",
            "完成 Hermes CLI 主动建议闭环。",
        )
        item = ItemState(
            "item-001",
            "完成 Hermes 验收",
            "in_progress",
            "完成真实 Hermes 验收。",
            "状态写入、判断和续跑均有证据。",
            "Connector 已实现。",
            "运行端到端测试。",
        )
        return OrganizationPlan(
            project,
            item,
            OrganizedTurn(
                project.id,
                item.id,
                "确认 Connector 已实现，端到端测试待运行。",
                ItemUpdate(
                    {
                        "current_progress": "Connector 已实现。",
                        "next_step": "运行端到端测试。",
                    }
                ),
            ),
            "QA 与 Hermes 验收事项目标一致。",
        )


class FixedJudge:
    def __init__(self, result: JudgeResult) -> None:
        self.result = result
        self.calls = 0

    def judge(self, *args: object) -> JudgeResult:
        del args
        self.calls += 1
        return self.result


class RecordingConnector:
    connector_id = "hermes-cli"

    def __init__(self) -> None:
        self.suggestions: list[SuggestionReady] = []
        self.resumes: list[SessionResumeRequested] = []

    def show_suggestion(self, event: SuggestionReady) -> None:
        self.suggestions.append(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        self.resumes.append(event)


def turn(*, turn_id: str = "turn-1", source: str | None = None) -> TurnCompleted:
    return TurnCompleted(
        platform="hermes-cli",
        session_id="session-1",
        turn_id=turn_id,
        user_question="记录：Connector 已实现，完整测试还没跑。",
        final_answer="已记录，下一步是运行完整测试。",
        completed_at=NOW,
        source_suggestion_id=source,
    )


class CoreClosureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(data_root)
        self.journal = RuntimeJournal(data_root)
        self.connector = RecordingConnector()

    def core(self, judge: FixedJudge) -> ProactiveAgentCore:
        return ProactiveAgentCore(
            self.store,
            self.journal,
            FixedOrganizer(),
            judge,
            ConnectorRouter((self.connector,)),
            asynchronous=False,
            log=lambda message: None,
        )

    def test_suggestion_path_updates_markdown_and_records_reason(self) -> None:
        judge = FixedJudge(
            JudgeResult(
                True,
                "实现完成但验证尚未执行。",
                "运行端到端测试",
                "Item 下一步是运行端到端测试。",
                "运行端到端测试并报告结果。",
            )
        )
        core = self.core(judge)
        core.handle(
            TurnStarted("hermes-cli", "session-1", "turn-1", NOW - timedelta(minutes=1))
        )
        core.handle(turn())

        self.assertEqual(self.store.read_item("project-001", "item-001").next_step, "运行端到端测试。")
        self.assertIn("Connector 已实现", self.store.read_events("project-001", "item-001"))
        decisions = self.journal.records("judge.decision")
        self.assertEqual(decisions[-1].payload["outcome"], "suggest")
        self.assertEqual(decisions[-1].payload["reason"], "实现完成但验证尚未执行。")
        self.assertEqual(len(self.connector.suggestions), 1)

    def test_silent_reason_is_recorded(self) -> None:
        core = self.core(FixedJudge(JudgeResult(False, "只有状态记录，没有额外行动价值。")))
        core.handle(turn())

        decision = self.journal.records("judge.decision")[-1]
        self.assertEqual(decision.payload["outcome"], "silent")
        self.assertEqual(decision.payload["reason"], "只有状态记录，没有额外行动价值。")
        self.assertEqual(self.connector.suggestions, [])

    def test_newer_user_turn_discards_old_judge_but_keeps_state(self) -> None:
        judge = FixedJudge(JudgeResult(True, "应提醒", "标题", "证据", "动作"))
        core = self.core(judge)
        core.handle(TurnStarted("hermes-cli", "session-1", "turn-2", NOW))
        core.handle(turn(turn_id="turn-1"))

        decision = self.journal.records("judge.decision")[-1]
        self.assertEqual(decision.payload["outcome"], "discarded")
        self.assertEqual(judge.calls, 0)
        self.assertTrue(
            self.store.read_events("project-001", "item-001").count("<!-- event:start ")
        )

    def test_authorized_result_only_updates_state_and_does_not_chain(self) -> None:
        judge = FixedJudge(JudgeResult(True, "不应调用", "标题", "证据", "动作"))
        core = self.core(judge)
        core.handle(turn(source="suggestion-original"))

        decision = self.journal.records("judge.decision")[-1]
        self.assertEqual(decision.payload["outcome"], "source_suggestion_completed")
        self.assertEqual(judge.calls, 0)
        self.assertEqual(self.connector.suggestions, [])

    def test_approve_dispatches_visible_resume_request_once(self) -> None:
        core = self.core(
            FixedJudge(JudgeResult(True, "值得执行", "运行测试", "尚未测试", "运行测试"))
        )
        core.handle(turn())
        suggestion_id = self.connector.suggestions[0].suggestion_id
        response = SuggestionResponded(suggestion_id, SuggestionChoice.APPROVE, NOW)

        core.handle(response)
        core.handle(response)

        self.assertEqual(len(self.connector.resumes), 1)
        self.assertEqual(self.connector.resumes[0].target_session_id, "session-1")


if __name__ == "__main__":
    unittest.main()
