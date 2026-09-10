from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from sn_proactive_agent.contracts import (
    SuggestionReady,
    TurnCompleted,
    TurnStarted,
)
from sn_proactive_agent.journal import JournalError, RuntimeJournal
from sn_proactive_agent.storage import event_id_for_turn


class RuntimeJournalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.journal = RuntimeJournal(Path(self.temporary_directory.name) / "data")

    def test_turns_are_idempotent_and_activity_survives_reload(self) -> None:
        started = TurnStarted(
            platform="hermes-cli",
            session_id="session-1",
            turn_id="turn-1",
            started_at=datetime(2026, 8, 25, 1, 0, tzinfo=timezone.utc),
        )
        completed = TurnCompleted(
            platform="hermes-cli",
            session_id="session-1",
            turn_id="turn-1",
            user_question="记录当前进展。",
            final_answer="已记录。",
            completed_at=datetime(2026, 8, 25, 1, 1, tzinfo=timezone.utc),
        )

        self.assertTrue(self.journal.append_turn_started(started))
        self.assertFalse(self.journal.append_turn_started(started))
        self.assertTrue(self.journal.append_turn_completed(completed))
        self.assertFalse(self.journal.append_turn_completed(completed))

        reloaded = RuntimeJournal(self.journal.data_root)
        self.assertEqual(
            reloaded.latest_started_turn("hermes-cli", "session-1"),
            "turn-1",
        )
        self.assertEqual(len(reloaded.records("turn.completed")), 1)

        reloaded.append(
            "organizer.result",
            f"organized-{event_id_for_turn(completed).removeprefix('event-')}",
            {
                "event_id": event_id_for_turn(completed),
                "outcome": "assigned",
                "project_id": "project-001",
                "item_id": "item-001",
            },
        )
        self.assertEqual(
            reloaded.latest_assignment("hermes-cli", "session-1"),
            ("project-001", "item-001"),
        )

    def test_suggestion_can_be_reloaded_from_jsonl(self) -> None:
        suggestion = SuggestionReady(
            suggestion_id="suggestion-1",
            platform="hermes-cli",
            created_at=datetime(2026, 8, 25, 1, 2, tzinfo=timezone.utc),
            target_session_id="session-1",
            title="运行测试",
            why_now="实现已经完成。",
            evidence="测试尚未运行。",
            suggested_action="运行完整测试并报告结果。",
        )
        self.journal.append(
            "suggestion.ready",
            suggestion.suggestion_id,
            suggestion.to_payload(),
            recorded_at=suggestion.created_at,
        )

        self.assertEqual(self.journal.get_suggestion("suggestion-1"), suggestion)

    def test_runtime_file_is_jsonl_and_has_no_markdown_wrapper(self) -> None:
        self.journal.append(
            "judge.decision",
            "decision-1",
            {"event_id": "event-1", "outcome": "silent", "reason": "已完成。"},
        )
        self.journal.append(
            "suggestion.ready",
            "suggestion-1",
            {"suggestion_id": "suggestion-1", "title": "继续推进"},
        )

        self.assertEqual(self.journal.path.name, "runtime.jsonl")
        lines = self.journal.path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(isinstance(json.loads(line), dict) for line in lines))
        self.assertNotIn("<!--", self.journal.path.read_text(encoding="utf-8"))

    def test_malformed_jsonl_is_rejected(self) -> None:
        self.journal.path.write_text('{"record_id":"broken"}\n', encoding="utf-8")

        with self.assertRaises(JournalError):
            self.journal.records()

    def test_duplicate_record_id_is_rejected(self) -> None:
        line = json.dumps(
            {
                "record_id": "decision-1",
                "kind": "judge.decision",
                "recorded_at": "2026-08-25T01:00:00+00:00",
                "payload": {},
            }
        )
        self.journal.path.write_text(f"{line}\n{line}\n", encoding="utf-8")

        with self.assertRaises(JournalError):
            self.journal.records()

    def test_legacy_markdown_requires_explicit_migration(self) -> None:
        legacy_path = self.journal.data_root / "runtime.md"
        self.journal.path.unlink()
        legacy_path.write_text("# Runtime Journal\n", encoding="utf-8")

        with self.assertRaisesRegex(JournalError, "migrate_runtime_journal"):
            RuntimeJournal(self.journal.data_root)


if __name__ == "__main__":
    unittest.main()
