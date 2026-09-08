from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from proactive_memory_service.api import create_app
from proactive_memory_service.bridge import BridgeHub
from proactive_memory_service.contracts import TurnCompleted, TurnStarted
from proactive_memory_service.hermes_bridge_install import instance_id
from proactive_memory_service.lifecycle import run_doctor
from test_api import request


class BridgeDiagnosticsTests(unittest.TestCase):
    def setUp(self):
        self.now = 1.0
        self.hub = BridgeHub(clock=lambda: self.now)
        self.app = create_app(bridge=self.hub)
        self.identity = {"platform": "hermes-tui", "session_id": "session-a", "client_id": "window-a", "instance_id": "install-a"}

    def heartbeat(self):
        _, first = request(self.app, "POST", "/v1/bridge/heartbeat", {**self.identity, "ready": True})
        _, second = request(self.app, "POST", "/v1/bridge/heartbeat", {**self.identity, "ready": True, "probe": first["probe"]})
        self.assertTrue(second["roundtrip"])

    def snapshot(self, **overrides):
        identity = {k: v for k, v in self.identity.items() if k != "client_id"}
        return self.hub.diagnostics(**{**identity, **overrides})

    def approve_fixture(self):
        self.hub.queue_resume_source("hermes-tui", "session-a", "suggestion-a")
        self.hub.publish("session.resume.requested", "hermes-tui", "session-a", {
            "suggestion_id": "suggestion-a", "platform": "hermes-tui",
            "target_session_id": "session-a", "suggested_action": "Update the weekly report",
        })

    def claim(self):
        return request(self.app, "POST", "/v1/bridge/resume/claim", {**self.identity, "suggestion_id": "suggestion-a"})

    def complete(self, source=None):
        base = {"platform": "hermes-tui", "session_id": "session-a", "turn_id": "turn-a"}
        if source:
            request(self.app, "POST", "/v1/bridge/source/claim", {
                **base, "user_question": "Update the weekly report",
            })
        request(self.app, "POST", "/v1/events/turn.started", {**base, "started_at": "2026-09-08T01:00:00Z"})
        body = {**base, "user_question": "Update the weekly report", "final_answer": "The report is updated.", "completed_at": "2026-09-08T01:01:00Z"}
        if source:
            body["source_suggestion_id"] = source
        status, _ = request(self.app, "POST", "/v1/events/turn.completed", body)
        self.assertEqual(status, 202)

    def test_live_roundtrip_is_not_execution_evidence(self):
        self.heartbeat()
        snapshot = self.snapshot()
        self.assertTrue(snapshot["online"])
        self.assertTrue(snapshot["roundtrip"])
        self.assertIsNone(snapshot["turn_completed"])
        self.assertIsNone(snapshot["resume_completed"])

    def test_approved_claimed_action_and_same_session_result_are_required(self):
        self.heartbeat()
        self.complete("suggestion-a")
        self.assertIsNone(self.snapshot()["resume_completed"])
        self.approve_fixture()
        self.complete("suggestion-a")
        self.assertIsNone(self.snapshot()["resume_completed"])
        self.assertTrue(self.claim()[1]["claimed"])
        self.complete("suggestion-a")
        self.assertEqual(self.snapshot()["resume_completed"], "suggestion-a")

    def test_claim_is_at_most_once_and_requires_prior_approval(self):
        self.heartbeat()
        self.assertFalse(self.claim()[1]["claimed"])
        self.approve_fixture()
        self.assertTrue(self.claim()[1]["claimed"])
        self.assertFalse(self.claim()[1]["claimed"])

    def test_republished_resume_does_not_reset_at_most_once_claim(self):
        self.heartbeat()
        self.approve_fixture()
        self.assertTrue(self.claim()[1]["claimed"])
        self.approve_fixture()
        self.assertFalse(self.claim()[1]["claimed"])

    def test_other_live_window_cannot_take_over_or_claim(self):
        self.heartbeat()
        self.approve_fixture()
        other = {**self.identity, "client_id": "other-window"}
        self.assertEqual(request(self.app, "POST", "/v1/bridge/heartbeat", other)[0], 400)
        self.assertEqual(request(self.app, "POST", "/v1/bridge/resume/claim", {**other, "suggestion_id": "suggestion-a"})[0], 400)

    def test_wrong_home_and_expired_window_do_not_pass(self):
        self.heartbeat()
        self.complete()
        self.assertFalse(self.snapshot(instance_id="other-install")["online"])
        self.now += 16
        self.assertFalse(self.snapshot()["online"])
        self.assertIsNone(self.snapshot()["turn_completed"])

    def test_user_turn_cannot_steal_suggestion_source(self):
        self.heartbeat()
        self.approve_fixture()
        self.assertIsNone(self.hub.claim_resume_source("hermes-tui", "session-a", user_question="Update the weekly report"))
        self.claim()
        self.assertIsNone(self.hub.claim_resume_source("hermes-tui", "session-a", user_question="A different question"))
        self.assertEqual(self.hub.claim_resume_source("hermes-tui", "session-a", user_question="Update the weekly report"), "suggestion-a")

    def test_failure_discards_source_and_cannot_later_count_as_resume(self):
        self.heartbeat()
        self.approve_fixture()
        self.claim()
        status, _ = request(self.app, "POST", "/v1/events/session.resume.failed", {
            "suggestion_id": "suggestion-a", "reason": "Session became busy",
            "failed_at": "2026-09-08T01:00:00Z",
        })
        self.assertEqual(status, 202)
        self.complete("suggestion-a")
        self.assertIsNone(self.snapshot()["resume_completed"])
        self.assertIsNone(self.hub.claim_resume_source("hermes-tui", "session-a"))

    def test_diagnostics_exclude_qa_and_are_read_only(self):
        self.heartbeat()
        self.complete()
        before = self.snapshot()
        status, body = request(self.app, "GET", "/v1/bridge/diagnostics?platform=hermes-tui&session_id=session-a&instance_id=install-a")
        self.assertEqual(status, 200)
        self.assertEqual(body, before)
        self.assertNotIn("weekly report", str(body))
        self.assertNotIn("final_answer", body)

    def test_invalid_probe_payload_is_rejected(self):
        for value in (None, [], {**self.identity, "ready": "true"}, {**self.identity, "client_id": "x" * 300}):
            with self.subTest(value=value):
                self.assertEqual(request(self.app, "POST", "/v1/bridge/heartbeat", value)[0], 400)
        self.assertEqual(request(self.app, "GET", "/v1/bridge/heartbeat")[0], 405)

    def test_doctor_uses_exact_session_and_instance_proof(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            self.identity["instance_id"] = instance_id(home)
            self.heartbeat()
            self.approve_fixture()
            self.claim()
            self.complete("suggestion-a")

            class Response:
                status = 200
                def __init__(self, body): self.body = body
                def __enter__(self): return self
                def __exit__(self, *_): pass
                def read(self, *_):
                    import json
                    return json.dumps(self.body).encode()

            def open_request(req, **_):
                from urllib.parse import urlsplit
                url = urlsplit(req.full_url)
                _, body = request(self.app, "GET", url.path + ("?" + url.query if url.query else ""))
                return Response(body)

            with patch("urllib.request.urlopen", side_effect=open_request):
                report = run_doctor(harness="hermes", hermes_home=home, data_root=home,
                    service_url="http://local.test", session_id="session-a")
                wrong = run_doctor(harness="hermes", hermes_home=home, data_root=home,
                    service_url="http://local.test", session_id="another-session")
            checks = {c.name: c for c in report.checks}
            self.assertTrue(checks["hermes_turn_capture"].ok)
            self.assertTrue(checks["hermes_same_session_resume"].ok)
            self.assertFalse(next(c for c in wrong.checks if c.name == "hermes_same_session_resume").ok)


if __name__ == "__main__":
    unittest.main()
