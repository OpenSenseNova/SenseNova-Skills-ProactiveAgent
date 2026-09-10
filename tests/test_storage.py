from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sn_proactive_agent.contracts import TurnCompleted
from sn_proactive_agent.storage import (
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
    StorageConflictError,
)


def completed_turn(*, turn_id: str = "turn-1") -> TurnCompleted:
    return TurnCompleted(
        platform="hermes",
        session_id="session-1",
        turn_id=turn_id,
        user_question="项目数据应该怎样保存？",
        final_answer="每个 Item 使用独立目录保存最新状态和 QA 历史。",
        completed_at=datetime(2026, 8, 24, 8, 0, tzinfo=timezone.utc),
    )


def project() -> ProjectMetadata:
    return ProjectMetadata(
        id="project-001",
        name="Proactive Agent",
        summary="将主动记忆 Demo 封装为可安装能力。",
        status="active",
    )


def item(*, item_id: str = "item-001", name: str = "确定存储结构") -> ItemState:
    return ItemState(
        id=item_id,
        name=name,
        status="in_progress",
        goal="确定 Project、Item 和 Event 的 Markdown 存储方式。",
        completion_criteria="目录结构和文件职责得到确认。",
        current_progress="Project 和 Item 目录已经确定。",
        next_step="确定 Event 格式。",
        blocker="等待 Event 设计确认。",
    )


def organized(
    *,
    item_id: str = "item-001",
    progress: str = "Event 格式已经确定。",
) -> OrganizedTurn:
    return OrganizedTurn(
        project_id="project-001",
        item_id=item_id,
        summary="本轮确认 Event 保存摘要、状态更新和完整原始 QA。",
        updates=ItemUpdate(
            {
                "current_progress": progress,
                "next_step": "实现 Markdown 存储闭环。",
                "blocker": None,
            }
        ),
    )


class MarkdownStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.store.create_project(project())
        self.store.create_item("project-001", item())

    def test_project_index_and_item_files_follow_confirmed_layout(self) -> None:
        item_directory = (
            self.data_root / "projects" / "project-001" / "items" / "item-001"
        )

        self.assertTrue((item_directory / "item.md").is_file())
        self.assertEqual(
            (item_directory / "events.md").read_text(encoding="utf-8"),
            "# Events\n",
        )
        self.assertEqual(self.store.list_projects(), (project(),))

        record = self.store.read_project("project-001")
        self.assertEqual(record.metadata, project())
        self.assertEqual(record.items[0].id, "item-001")
        self.assertEqual(record.items[0].name, "确定存储结构")
        self.assertEqual(self.store.read_item("project-001", "item-001"), item())

        project_markdown = (
            self.data_root / "projects" / "project-001" / "project.md"
        ).read_text(encoding="utf-8")
        self.assertIn("[item.md](items/item-001/item.md)", project_markdown)
        self.assertIn("[events.md](items/item-001/events.md)", project_markdown)

    def test_apply_turn_appends_event_before_updating_latest_item_state(self) -> None:
        result = self.store.apply_turn(completed_turn(), organized())

        self.assertTrue(result.appended)
        self.assertEqual(result.item.current_progress, "Event 格式已经确定。")
        self.assertEqual(result.item.next_step, "实现 Markdown 存储闭环。")
        self.assertIsNone(result.item.blocker)

        event_log = self.store.read_events("project-001", "item-001")
        self.assertIn(f"<!-- event:start {result.event_id} -->", event_log)
        self.assertIn('platform: "hermes"', event_log)
        self.assertIn('session_id: "session-1"', event_log)
        self.assertIn('turn_id: "turn-1"', event_log)
        self.assertIn("项目数据应该怎样保存？", event_log)
        self.assertIn("每个 Item 使用独立目录", event_log)

        item_markdown = (
            self.data_root
            / "projects"
            / "project-001"
            / "items"
            / "item-001"
            / "item.md"
        ).read_text(encoding="utf-8")
        self.assertNotIn("## 阻塞", item_markdown)

    def test_duplicate_turn_reuses_first_event_updates(self) -> None:
        first = self.store.apply_turn(completed_turn(), organized())
        second = self.store.apply_turn(
            completed_turn(),
            organized(progress="这份不同的重试结果不应覆盖第一次记录。"),
        )

        self.assertTrue(first.appended)
        self.assertFalse(second.appended)
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(second.item.current_progress, "Event 格式已经确定。")
        event_log = self.store.read_events("project-001", "item-001")
        self.assertEqual(event_log.count("<!-- event:start "), 1)

    def test_same_turn_cannot_be_written_to_a_different_item(self) -> None:
        self.store.create_item(
            "project-001",
            item(item_id="item-002", name="实现存储闭环"),
        )
        self.store.apply_turn(completed_turn(), organized())

        with self.assertRaisesRegex(StorageConflictError, "already assigned"):
            self.store.apply_turn(
                completed_turn(),
                organized(item_id="item-002"),
            )

        self.assertEqual(
            self.store.read_events("project-001", "item-002"), "# Events\n"
        )

    def test_retry_recovers_when_event_was_written_but_item_update_failed(self) -> None:
        original_write_item = self.store._write_item
        should_fail = True

        def fail_once(project_id: str, state: ItemState) -> None:
            nonlocal should_fail
            if should_fail:
                should_fail = False
                raise OSError("simulated item write failure")
            original_write_item(project_id, state)

        self.store._write_item = fail_once  # type: ignore[method-assign]

        with self.assertRaisesRegex(OSError, "simulated item write failure"):
            self.store.apply_turn(completed_turn(), organized())

        self.assertEqual(
            self.store.read_item("project-001", "item-001").current_progress,
            "Project 和 Item 目录已经确定。",
        )
        self.assertEqual(
            self.store.read_events("project-001", "item-001").count(
                "<!-- event:start "
            ),
            1,
        )

        recovered = self.store.apply_turn(completed_turn(), organized())

        self.assertFalse(recovered.appended)
        self.assertEqual(recovered.item.current_progress, "Event 格式已经确定。")
        self.assertEqual(
            self.store.read_events("project-001", "item-001").count(
                "<!-- event:start "
            ),
            1,
        )

    def test_path_traversal_ids_are_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "lowercase letters"):
            self.store.read_project("../outside")

    def test_item_status_is_limited_to_the_v1_states(self) -> None:
        with self.assertRaisesRegex(ValueError, "item status must be one of"):
            ItemState(
                id="item-invalid",
                name="非法状态",
                status="unknown",
                goal="验证状态",
                completion_criteria="状态有效",
                current_progress="尚未开始",
                next_step="设置状态",
            )


if __name__ == "__main__":
    unittest.main()
