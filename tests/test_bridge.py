from __future__ import annotations

import unittest
from threading import Thread
from time import sleep

from sn_proactive_agent.bridge import BridgeHub


class BridgeHubTests(unittest.TestCase):
    def test_poll_returns_ordered_session_events_and_cursor(self) -> None:
        hub = BridgeHub()
        first = hub.publish("suggestion.ready", "hermes-tui", "session-1", {"n": 1})
        second = hub.publish(
            "session.resume.requested", "hermes-tui", "session-1", {"n": 2}
        )
        hub.publish("suggestion.ready", "hermes-tui", "session-2", {"n": 3})

        cursor, records = hub.poll("hermes-tui", "session-1", after=0)

        self.assertEqual(cursor, second.sequence)
        self.assertEqual([record.sequence for record in records], [first.sequence, second.sequence])
        self.assertEqual(records[0].to_payload()["event_type"], "suggestion.ready")

    def test_poll_waits_for_a_new_event(self) -> None:
        hub = BridgeHub()
        result: list[tuple[int, tuple[object, ...]]] = []

        def wait_for_event() -> None:
            cursor, records = hub.poll(
                "hermes-tui",
                "session-1",
                after=0,
                timeout_seconds=1,
            )
            result.append((cursor, records))

        thread = Thread(target=wait_for_event)
        thread.start()
        sleep(0.02)
        hub.publish("suggestion.ready", "hermes-tui", "session-1", {"ok": True})
        thread.join(timeout=1)

        self.assertFalse(thread.is_alive())
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0][1]), 1)

    def test_resume_source_is_claimed_once(self) -> None:
        hub = BridgeHub()
        hub.queue_resume_source("hermes-tui", "session-1", "suggestion-1")

        self.assertEqual(
            hub.claim_resume_source("hermes-tui", "session-1"),
            "suggestion-1",
        )
        self.assertIsNone(hub.claim_resume_source("hermes-tui", "session-1"))


if __name__ == "__main__":
    unittest.main()
