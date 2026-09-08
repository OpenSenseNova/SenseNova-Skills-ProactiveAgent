from __future__ import annotations

import unittest
from dataclasses import dataclass, field
from io import StringIO
from pathlib import Path
from threading import Event
from tempfile import TemporaryDirectory
from unittest.mock import patch

import proactive_memory_service.connector as connector_module

from proactive_memory_service.connector import (
    ConnectorConflictError,
    ConnectorNotFoundError,
    ConnectorRouter,
    HermesCliConnector,
    HermesTuiConnector,
)
from proactive_memory_service.bridge import BridgeHub
from proactive_memory_service.contracts import (
    EventType,
    SessionResumeRequested,
    SuggestionReady,
    parse_event,
)

from test_contracts import EVENT_SAMPLES


@dataclass
class RecordingConnector:
    connector_id: str
    suggestions: list[SuggestionReady] = field(default_factory=list)
    resume_requests: list[SessionResumeRequested] = field(default_factory=list)

    def show_suggestion(self, event: SuggestionReady) -> None:
        self.suggestions.append(event)

    def resume_session(self, event: SessionResumeRequested) -> None:
        self.resume_requests.append(event)


class ConnectorRouterTests(unittest.TestCase):
    def test_routes_outbound_events_by_open_connector_identifier(self) -> None:
        connector = RecordingConnector("deepseek-harness")
        router = ConnectorRouter((connector,))

        suggestion_payload = dict(EVENT_SAMPLES[EventType.SUGGESTION_READY])
        suggestion_payload["platform"] = connector.connector_id
        suggestion = parse_event(EventType.SUGGESTION_READY, suggestion_payload)

        resume_payload = dict(EVENT_SAMPLES[EventType.SESSION_RESUME_REQUESTED])
        resume_payload["platform"] = connector.connector_id
        resume = parse_event(EventType.SESSION_RESUME_REQUESTED, resume_payload)

        assert isinstance(suggestion, SuggestionReady)
        assert isinstance(resume, SessionResumeRequested)
        router.dispatch(suggestion)
        router.dispatch(resume)

        self.assertEqual(connector.suggestions, [suggestion])
        self.assertEqual(connector.resume_requests, [resume])

    def test_duplicate_connector_identifier_is_rejected(self) -> None:
        with self.assertRaisesRegex(
            ConnectorConflictError,
            "connector already registered: codex",
        ):
            ConnectorRouter((RecordingConnector("codex"), RecordingConnector("codex")))

    def test_unregistered_connector_is_reported(self) -> None:
        event = parse_event(
            EventType.SUGGESTION_READY,
            EVENT_SAMPLES[EventType.SUGGESTION_READY],
        )
        assert isinstance(event, SuggestionReady)

        with self.assertRaisesRegex(
            ConnectorNotFoundError,
            "connector is not registered: openclaw",
        ):
            ConnectorRouter().dispatch(event)

    def test_hermes_connector_renders_card_and_resumes_exact_session(self) -> None:
        output = StringIO()
        finished = Event()
        calls: list[tuple[list[str], dict[str, object]]] = []

        class FakeProcess:
            def wait(self) -> int:
                finished.set()
                return 0

        def process_factory(command: list[str], **kwargs: object) -> FakeProcess:
            calls.append((command, kwargs))
            return FakeProcess()

        connector = HermesCliConnector(
            "/opt/hermes",
            output=output,
            process_factory=process_factory,  # type: ignore[arg-type]
            provider="openai-codex",
            model="gpt-5.6-luna",
        )
        suggestion = parse_event(
            EventType.SUGGESTION_READY,
            {
                **EVENT_SAMPLES[EventType.SUGGESTION_READY],
                "platform": "hermes-cli",
            },
        )
        resume = parse_event(
            EventType.SESSION_RESUME_REQUESTED,
            {
                **EVENT_SAMPLES[EventType.SESSION_RESUME_REQUESTED],
                "platform": "hermes-cli",
            },
        )
        assert isinstance(suggestion, SuggestionReady)
        assert isinstance(resume, SessionResumeRequested)

        connector.show_suggestion(suggestion)
        connector.resume_session(resume)
        self.assertTrue(finished.wait(timeout=1))

        self.assertIn("为什么现在", output.getvalue())
        self.assertIn(" approve ", output.getvalue())
        command, kwargs = calls[0]
        self.assertIn("--resume", command)
        self.assertIn("session-1", command)
        self.assertEqual(command[-4:], ["--provider", "openai-codex", "--model", "gpt-5.6-luna"])
        environment = kwargs["env"]
        assert isinstance(environment, dict)
        self.assertEqual(
            environment["PROACTIVE_MEMORY_SOURCE_SUGGESTION_ID"],
            "suggestion-1",
        )

    def test_source_checkout_respond_command_uses_python_module(self) -> None:
        output = StringIO()
        connector = HermesCliConnector("/opt/hermes", output=output)
        suggestion = parse_event(
            EventType.SUGGESTION_READY,
            {
                **EVENT_SAMPLES[EventType.SUGGESTION_READY],
                "platform": "hermes-cli",
            },
        )
        assert isinstance(suggestion, SuggestionReady)

        with patch.object(
            connector_module.shutil,
            "which",
            return_value="/usr/local/bin/proactive-memory-service",
        ):
            connector.show_suggestion(suggestion)

        rendered = output.getvalue()
        self.assertIn(
            "python3 -m proactive_memory_service respond suggestion-1 approve",
            rendered,
        )
        self.assertNotIn(
            "proactive-memory-service proactive_memory_service respond",
            rendered,
        )

    def test_installed_connector_uses_console_script_for_response_command(self) -> None:
        """A wheel/pipx package must not fall back to an unrelated python3."""
        with TemporaryDirectory() as temp_dir:
            fake_module = (
                Path(temp_dir) / "site-packages" / "proactive_memory_service" / "connector.py"
            )
            fake_module.parent.mkdir(parents=True)
            fake_module.touch()

            output = StringIO()
            connector = HermesCliConnector("/opt/hermes", output=output)
            suggestion = parse_event(
                EventType.SUGGESTION_READY,
                {
                    **EVENT_SAMPLES[EventType.SUGGESTION_READY],
                    "platform": "hermes-cli",
                },
            )
            assert isinstance(suggestion, SuggestionReady)

            with (
                patch.object(connector_module, "__file__", str(fake_module)),
                patch.object(
                    connector_module.shutil,
                    "which",
                    return_value="/pipx/venv/bin/proactive-memory-service",
                ),
            ):
                connector.show_suggestion(suggestion)

        rendered = output.getvalue()
        self.assertIn(
            "proactive-memory-service respond suggestion-1 approve",
            rendered,
        )
        self.assertIn(
            "proactive-memory-service respond suggestion-1 ignore",
            rendered,
        )
        self.assertNotIn(
            "proactive-memory-service proactive_memory_service",
            rendered,
        )
        self.assertNotIn("python3 -m", rendered)

    def test_installed_module_fallback_uses_loaded_interpreter(self) -> None:
        """A direct ``python -m`` launch stays in the same environment."""
        with TemporaryDirectory() as temp_dir:
            fake_module = (
                Path(temp_dir) / "site-packages" / "proactive_memory_service" / "connector.py"
            )
            fake_module.parent.mkdir(parents=True)
            fake_module.touch()
            connector = HermesCliConnector("/opt/hermes")

            with (
                patch.object(connector_module, "__file__", str(fake_module)),
                patch.object(connector_module.shutil, "which", return_value=None),
                patch.object(connector_module.sys, "executable", "/venv/bin/python"),
            ):
                self.assertEqual(
                    connector._respond_prefix(),
                    "/venv/bin/python -m proactive_memory_service",
                )

    def test_tui_connector_publishes_session_scoped_events_without_spawning(self) -> None:
        hub = BridgeHub()
        connector = HermesTuiConnector(hub, log=lambda _message: None)
        suggestion = parse_event(
            EventType.SUGGESTION_READY,
            {**EVENT_SAMPLES[EventType.SUGGESTION_READY], "platform": "hermes-tui"},
        )
        resume = parse_event(
            EventType.SESSION_RESUME_REQUESTED,
            {**EVENT_SAMPLES[EventType.SESSION_RESUME_REQUESTED], "platform": "hermes-tui"},
        )
        assert isinstance(suggestion, SuggestionReady)
        assert isinstance(resume, SessionResumeRequested)

        connector.show_suggestion(suggestion)
        connector.resume_session(resume)

        cursor, records = hub.poll("hermes-tui", "session-1")
        self.assertEqual(cursor, 2)
        self.assertEqual([record.event_type for record in records], [
            "suggestion.ready",
            "session.resume.requested",
        ])
        self.assertEqual(hub.claim_resume_source("hermes-tui", "session-1"), "suggestion-1")

    def test_web_only_tui_hides_suggestion_but_keeps_resume_bridge(self) -> None:
        hub = BridgeHub()
        connector = HermesTuiConnector(
            hub,
            log=lambda _message: None,
            show_suggestions=False,
        )
        suggestion = parse_event(
            EventType.SUGGESTION_READY,
            {**EVENT_SAMPLES[EventType.SUGGESTION_READY], "platform": "hermes-tui"},
        )
        resume = parse_event(
            EventType.SESSION_RESUME_REQUESTED,
            {**EVENT_SAMPLES[EventType.SESSION_RESUME_REQUESTED], "platform": "hermes-tui"},
        )
        assert isinstance(suggestion, SuggestionReady)
        assert isinstance(resume, SessionResumeRequested)

        connector.show_suggestion(suggestion)
        connector.resume_session(resume)

        cursor, records = hub.poll("hermes-tui", "session-1")
        self.assertEqual(cursor, 1)
        self.assertEqual([record.event_type for record in records], ["session.resume.requested"])
        self.assertEqual(hub.claim_resume_source("hermes-tui", "session-1"), "suggestion-1")


if __name__ == "__main__":
    unittest.main()
