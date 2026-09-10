from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from types import ModuleType


def load_hook() -> ModuleType:
    path = (
        Path(__file__).parents[1]
        / "src"
        / "sn_proactive_agent_connectors"
        / "hermes"
        / "classic"
        / "hermes_hook.py"
    )
    spec = importlib.util.spec_from_file_location("sn_proactive_agent_hermes_hook", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class HermesHookTests(unittest.TestCase):
    def test_pre_and_post_hooks_map_to_the_same_v1_identity(self) -> None:
        hook = load_hook()
        common = {
            "session_id": "session-1",
            "extra": {
                "turn_id": "turn-1",
                "user_message": "记录状态",
                "assistant_response": "状态已记录",
            },
        }

        started_name, started = hook._map_payload(
            {**common, "hook_event_name": "pre_llm_call"}
        )
        completed_name, completed = hook._map_payload(
            {**common, "hook_event_name": "post_llm_call"}
        )

        self.assertEqual(started_name, "turn.started")
        self.assertEqual(completed_name, "turn.completed")
        self.assertEqual(started["platform"], "hermes-cli")
        self.assertEqual(started["session_id"], completed["session_id"])
        self.assertEqual(started["turn_id"], completed["turn_id"])
        self.assertEqual(completed["user_question"], "记录状态")
        self.assertEqual(completed["final_answer"], "状态已记录")

    def test_tui_payload_is_left_to_native_plugin(self) -> None:
        hook = load_hook()
        event_name, payload = hook._map_payload(
            {
                "hook_event_name": "pre_llm_call",
                "session_id": "session-1",
                "extra": {"platform": "tui", "user_message": "继续"},
            }
        )
        self.assertIsNone(event_name)
        self.assertEqual(payload, {})


if __name__ == "__main__":
    unittest.main()
