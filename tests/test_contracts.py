from __future__ import annotations

import unittest

from proactive_memory_service.contracts import (
    ContractValidationError,
    EventType,
    SessionResumeFailed,
    SessionResumeRequested,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
    TurnStarted,
    event_to_envelope,
    parse_event,
)


EVENT_SAMPLES = {
    EventType.TURN_STARTED: {
        "platform": "codex",
        "session_id": "session-1",
        "turn_id": "turn-7",
        "started_at": "2026-08-24T07:59:00Z",
    },
    EventType.TURN_COMPLETED: {
        "platform": "hermes",
        "session_id": "session-1",
        "turn_id": "turn-7",
        "user_question": "请运行基础测试。",
        "final_answer": "基础测试已经通过。",
        "completed_at": "2026-08-24T08:00:00Z",
        "tool_execution_evidence": [
            {
                "tool_name": "shell",
                "action_summary": "运行单元测试",
                "status": "succeeded",
                "artifact_refs": ["tests/"],
            }
        ],
        "source_suggestion_id": "suggestion-1",
    },
    EventType.SUGGESTION_READY: {
        "suggestion_id": "suggestion-1",
        "platform": "openclaw",
        "created_at": "2026-08-24T08:01:00Z",
        "target_session_id": "session-1",
        "title": "补充接口测试",
        "why_now": "事件结构已经稳定。",
        "evidence": "当前只有数据结构测试。",
        "suggested_action": "为三个入站事件补充 HTTP 接口测试。",
    },
    EventType.SUGGESTION_RESPONDED: {
        "suggestion_id": "suggestion-1",
        "choice": "approve",
        "responded_at": "2026-08-24T08:02:00Z",
    },
    EventType.SESSION_RESUME_REQUESTED: {
        "suggestion_id": "suggestion-1",
        "platform": "hermes",
        "target_session_id": "session-1",
        "suggested_action": "为三个入站事件补充 HTTP 接口测试。",
    },
    EventType.SESSION_RESUME_FAILED: {
        "suggestion_id": "suggestion-1",
        "reason": "目标 Session 不可用。",
        "failed_at": "2026-08-24T08:03:00Z",
    },
}


class EventContractTests(unittest.TestCase):
    def test_all_six_events_round_trip(self) -> None:
        expected_models = {
            EventType.TURN_STARTED: TurnStarted,
            EventType.TURN_COMPLETED: TurnCompleted,
            EventType.SUGGESTION_READY: SuggestionReady,
            EventType.SUGGESTION_RESPONDED: SuggestionResponded,
            EventType.SESSION_RESUME_REQUESTED: SessionResumeRequested,
            EventType.SESSION_RESUME_FAILED: SessionResumeFailed,
        }

        for event_type, payload in EVENT_SAMPLES.items():
            with self.subTest(event_type=event_type.value):
                event = parse_event(event_type, payload)
                self.assertIsInstance(event, expected_models[event_type])
                self.assertEqual(event_to_envelope(event)["event_type"], event_type.value)
                self.assertEqual(event.to_payload(), payload)

    def test_platform_accepts_an_unregistered_connector_identifier(self) -> None:
        payload = dict(EVENT_SAMPLES[EventType.TURN_COMPLETED])
        payload["platform"] = "deepseek-harness"

        event = parse_event(EventType.TURN_COMPLETED, payload)

        self.assertIsInstance(event, TurnCompleted)
        self.assertEqual(event.platform, "deepseek-harness")
        self.assertEqual(event.to_payload()["platform"], "deepseek-harness")

    def test_turn_completed_requires_timezone(self) -> None:
        payload = dict(EVENT_SAMPLES[EventType.TURN_COMPLETED])
        payload["completed_at"] = "2026-08-24T08:00:00"

        with self.assertRaisesRegex(ContractValidationError, "include a timezone"):
            parse_event(EventType.TURN_COMPLETED, payload)

    def test_suggestion_responded_accepts_only_v1_choices(self) -> None:
        payload = dict(EVENT_SAMPLES[EventType.SUGGESTION_RESPONDED])
        payload["choice"] = "later"

        with self.assertRaisesRegex(ContractValidationError, "approve, ignore"):
            parse_event(EventType.SUGGESTION_RESPONDED, payload)

    def test_unknown_fields_are_rejected(self) -> None:
        payload = dict(EVENT_SAMPLES[EventType.SESSION_RESUME_FAILED])
        payload["retry"] = True

        with self.assertRaisesRegex(ContractValidationError, "unknown fields: retry"):
            parse_event(EventType.SESSION_RESUME_FAILED, payload)


if __name__ == "__main__":
    unittest.main()
