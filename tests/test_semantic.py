from __future__ import annotations

import tempfile
import unittest
from collections import deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from sn_proactive_agent.contracts import TurnCompleted
from sn_proactive_agent.core import OrganizerContext
from sn_proactive_agent.semantic import (
    OrganizationPlan,
    SemanticJudge,
    SemanticOrganizer,
)


class ScriptedReasoner:
    def __init__(self, *responses: Mapping[str, Any]) -> None:
        self.responses = deque(responses)
        self.prompts: list[str] = []

    def ask_json(self, prompt: str) -> Mapping[str, Any]:
        self.prompts.append(prompt)
        return self.responses.popleft()


def completed_turn() -> TurnCompleted:
    return TurnCompleted(
        platform="hermes-cli",
        session_id="session-1",
        turn_id="turn-1",
        user_question="Connector 已实现，但完整测试还没跑。",
        final_answer="已确认下一步需要运行完整测试。",
        completed_at=datetime(2026, 8, 25, 2, 0, tzinfo=timezone.utc),
    )


class SemanticTests(unittest.TestCase):
    def test_organizer_creates_first_project_and_item_from_two_stage_reasoning(self) -> None:
        reasoner = ScriptedReasoner(
            {
                "route": "new_project",
                "project_name": "Proactive Agent",
                "project_summary": "把主动记忆能力封装为跨 Harness 服务。",
                "reason": "QA 涉及持续推进的工程目标。",
            },
            {
                "item_route": "new_item",
                "item": {
                    "name": "完成 Hermes 验收",
                    "status": "in_progress",
                    "goal": "完成 Hermes 端到端验收。",
                    "completion_criteria": "真实 QA 被记录并产生正确判断。",
                    "current_progress": "Connector 已实现。",
                    "next_step": "运行完整测试。",
                    "blocker": None,
                },
                "event_summary": "确认 Connector 已实现且测试待运行。",
                "updates": {
                    "current_progress": "Connector 已实现。",
                    "next_step": "运行完整测试。",
                    "blocker": None,
                },
                "reason": "这是一个新的可完成事项。",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            result = SemanticOrganizer(reasoner).organize(
                completed_turn(),
                OrganizerContext(Path(directory) / "data", ()),
            )

        self.assertIsInstance(result, OrganizationPlan)
        assert isinstance(result, OrganizationPlan)
        self.assertEqual(result.project.id, "project-001")
        self.assertEqual(result.initial_item.id, "item-001")
        self.assertEqual(result.event.updates.values["next_step"], "运行完整测试。")
        self.assertEqual(len(reasoner.prompts), 2)
        self.assertIn("project_summary 不超过 48 个字符", reasoner.prompts[0])
        self.assertIn("project_name 不超过 24 个字符", reasoner.prompts[0])
        self.assertIn("current_progress 不超过 60 个字符", reasoner.prompts[1])
        self.assertIn("next_step 不超过 32 个字符", reasoner.prompts[1])
        self.assertIn("event_summary 不超过 80 个字符", reasoner.prompts[1])

    def test_judge_returns_a_reason_for_silent_and_suggest(self) -> None:
        silent = SemanticJudge(
            ScriptedReasoner(
                {"outcome": "silent", "reason": "只有状态记录，没有待执行动作。"}
            )
        )
        suggest = SemanticJudge(
            ScriptedReasoner(
                {
                    "outcome": "suggest",
                    "reason": "实现完成但验证尚未执行。",
                    "title": "运行完整测试",
                    "evidence": "下一步明确写着运行完整测试。",
                    "suggested_action": "运行完整测试并报告结果。",
                }
            )
        )
        from sn_proactive_agent.storage import ItemState, ProjectMetadata

        project = ProjectMetadata("project-001", "PMS", "主动记忆服务")
        item = ItemState(
            "item-001",
            "完成验收",
            "in_progress",
            "完成验收",
            "测试通过",
            "实现完成",
            "运行测试",
        )

        self.assertFalse(silent.judge(completed_turn(), project, item, "记录").should_suggest)
        self.assertTrue(suggest.judge(completed_turn(), project, item, "记录").should_suggest)
        self.assertIn("等待一个或多个外部输入", silent.reasoner.prompts[0])
        self.assertIn("title 不超过 24 个字符", suggest.reasoner.prompts[0])
        self.assertIn("suggested_action 不超过 80 个字符", suggest.reasoner.prompts[0])

    def test_empty_updates_are_repaired_once_before_storage(self) -> None:
        reasoner = ScriptedReasoner(
            {
                "route": "new_project",
                "project_name": "PMS",
                "project_summary": "完成主动记忆服务。",
                "reason": "持续工程。",
            },
            {
                "item_route": "new_item",
                "item": {
                    "name": "取消验证",
                    "status": "in_progress",
                    "goal": "验证取消",
                    "completion_criteria": "计划取消",
                    "current_progress": "等待取消",
                    "next_step": "等待更新",
                    "blocker": None,
                },
                "event_summary": "计划已取消。",
                "updates": {},
                "reason": "新事项。",
            },
            {
                "event_summary": "计划已取消并完成。",
                "updates": {
                    "status": "completed",
                    "current_progress": "计划已取消。",
                    "next_step": "无。",
                },
                "reason": "根据 QA 修复为空的更新。",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            result = SemanticOrganizer(reasoner).organize(
                completed_turn(), OrganizerContext(Path(directory) / "data", ())
            )

        assert isinstance(result, OrganizationPlan)
        self.assertEqual(result.event.updates.values["status"], "completed")
        self.assertEqual(len(reasoner.prompts), 3)
        self.assertIn("event_summary 不超过 80 个字符", reasoner.prompts[2])
        self.assertIn("不要静默截断", reasoner.prompts[2])

    def test_completed_item_is_silent_without_calling_model(self) -> None:
        from sn_proactive_agent.storage import ItemState, ProjectMetadata

        reasoner = ScriptedReasoner()
        result = SemanticJudge(reasoner).judge(
            completed_turn(),
            ProjectMetadata("project-001", "PMS", "主动记忆服务"),
            ItemState(
                "item-001",
                "已完成事项",
                "completed",
                "完成事项",
                "完成",
                "已完成",
                "无。",
            ),
            "事项完成。",
        )

        self.assertFalse(result.should_suggest)
        self.assertEqual(reasoner.prompts, [])

    def test_recent_item_hint_resolves_anaphoric_follow_up(self) -> None:
        from sn_proactive_agent.storage import ItemState, MarkdownStore, ProjectMetadata

        reasoner = ScriptedReasoner(
            {"route": "untracked", "reason": "单轮文本没有项目名。"},
            {
                "refers_to_recent_item": True,
                "reason": "“刚才那个事项”明确指向最近 Item。",
            },
            {
                "item_route": "existing_item",
                "item_id": "item-001",
                "event_summary": "刚才的读取计划已经取消。",
                "updates": {
                    "status": "completed",
                    "current_progress": "读取计划已取消。",
                    "next_step": "无。",
                },
                "reason": "复用最近 Item。",
            },
        )
        with tempfile.TemporaryDirectory() as directory:
            data_root = Path(directory) / "data"
            store = MarkdownStore(data_root)
            store.create_project(ProjectMetadata("project-001", "PMS", "主动记忆服务"))
            store.create_item(
                "project-001",
                ItemState(
                    "item-001",
                    "新输入取消验证",
                    "in_progress",
                    "验证取消",
                    "计划取消",
                    "等待授权",
                    "读取文件",
                ),
            )
            result = SemanticOrganizer(reasoner).organize(
                completed_turn(),
                OrganizerContext(
                    data_root,
                    store.list_projects(),
                    recent_project_id="project-001",
                    recent_item_id="item-001",
                ),
            )

        assert isinstance(result, OrganizationPlan)
        self.assertEqual(result.event.item_id, "item-001")
        self.assertEqual(result.event.updates.values["status"], "completed")
        self.assertEqual(len(reasoner.prompts), 3)


if __name__ == "__main__":
    unittest.main()
