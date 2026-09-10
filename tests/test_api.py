from __future__ import annotations

import json
import tempfile
import unittest
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

from sn_proactive_agent.api import create_app
from sn_proactive_agent.bridge import BridgeHub
from sn_proactive_agent.core import OrganizerContext, TurnStorageHandler
from sn_proactive_agent.contracts import EventType, InboundEvent
from sn_proactive_agent.storage import (
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)

from test_contracts import EVENT_SAMPLES


class RecordingHandler:
    def __init__(self) -> None:
        self.events: list[InboundEvent] = []

    def handle(self, event: InboundEvent) -> None:
        self.events.append(event)


class FixedOrganizer:
    def __init__(self) -> None:
        self.context: OrganizerContext | None = None

    def organize(
        self,
        event: Any,
        context: OrganizerContext,
    ) -> OrganizedTurn:
        del event
        self.context = context
        return OrganizedTurn(
            project_id="project-001",
            item_id="item-001",
            summary="基础接口测试已经运行。",
            updates=ItemUpdate(
                {
                    "current_progress": "HTTP 入站已驱动 Markdown 落盘。",
                    "next_step": "实现 Organizer 语义归类。",
                }
            ),
        )


def request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
    *,
    content_type: str = "application/json",
) -> tuple[int, dict[str, Any]]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    path, separator, query_string = path.partition("?")
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": path,
            "QUERY_STRING": query_string if separator else "",
            "CONTENT_TYPE": content_type,
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }
    )
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return int(captured["status"].split(" ", 1)[0]), json.loads(response_body)


class EventApiTests(unittest.TestCase):
    def setUp(self) -> None:
        self.handler = RecordingHandler()
        self.app = create_app(self.handler)

    def test_health(self) -> None:
        status, body = request(self.app, "GET", "/health")

        self.assertEqual(status, 200)
        self.assertEqual(body, {"status": "ok"})

    def test_all_four_inbound_event_interfaces_accept_valid_payloads(self) -> None:
        inbound_types = (
            EventType.TURN_STARTED,
            EventType.TURN_COMPLETED,
            EventType.SUGGESTION_RESPONDED,
            EventType.SESSION_RESUME_FAILED,
        )

        for event_type in inbound_types:
            with self.subTest(event_type=event_type.value):
                status, body = request(
                    self.app,
                    "POST",
                    f"/v1/events/{event_type.value}",
                    EVENT_SAMPLES[event_type],
                )
                self.assertEqual(status, 202)
                self.assertEqual(
                    body, {"accepted": True, "event_type": event_type.value}
                )

        self.assertEqual(len(self.handler.events), 4)

    def test_invalid_event_returns_422_without_reaching_handler(self) -> None:
        payload = dict(EVENT_SAMPLES[EventType.TURN_COMPLETED])
        del payload["turn_id"]

        status, body = request(
            self.app,
            "POST",
            "/v1/events/turn.completed",
            payload,
        )

        self.assertEqual(status, 422)
        self.assertEqual(body["error"], "invalid_event")
        self.assertEqual(self.handler.events, [])

    def test_outbound_events_are_not_exposed_as_inbound_routes(self) -> None:
        status, body = request(
            self.app,
            "POST",
            "/v1/events/suggestion.ready",
            EVENT_SAMPLES[EventType.SUGGESTION_READY],
        )

        self.assertEqual(status, 404)
        self.assertEqual(body, {"error": "not_found"})

    def test_turn_completed_can_drive_injected_markdown_storage_handler(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            store = MarkdownStore(Path(temporary_directory) / "data")
            store.create_project(
                ProjectMetadata(
                    id="project-001",
                    name="Proactive Agent",
                    summary="将主动记忆 Demo 封装为可安装能力。",
                )
            )
            store.create_item(
                "project-001",
                ItemState(
                    id="item-001",
                    name="实现存储闭环",
                    status="in_progress",
                    goal="接收完整 QA 并保存状态。",
                    completion_criteria="HTTP 入站能够写入 Item 和 Event。",
                    current_progress="契约接口已经存在。",
                    next_step="连接 Markdown 存储。",
                ),
            )
            organizer = FixedOrganizer()
            app = create_app(TurnStorageHandler(store, organizer))

            status, body = request(
                app,
                "POST",
                "/v1/events/turn.completed",
                EVENT_SAMPLES[EventType.TURN_COMPLETED],
            )

            self.assertEqual(status, 202)
            self.assertEqual(body["event_type"], "turn.completed")
            self.assertEqual(
                store.read_item("project-001", "item-001").current_progress,
                "HTTP 入站已驱动 Markdown 落盘。",
            )
            self.assertEqual(
                store.read_events("project-001", "item-001").count(
                    "<!-- event:start "
                ),
                1,
            )
            event_log = store.read_events("project-001", "item-001")
            self.assertIn('source_suggestion_id: "suggestion-1"', event_log)
            self.assertIn('"tool_name": "shell"', event_log)
            self.assertIsNotNone(organizer.context)
            assert organizer.context is not None
            self.assertEqual(organizer.context.data_root, store.data_root)
            self.assertEqual(
                tuple(project.id for project in organizer.context.projects),
                ("project-001",),
            )

    def test_tui_bridge_delivers_events_and_claims_resume_source(self) -> None:
        bridge = BridgeHub()
        bridge.publish(
            "suggestion.ready",
            "hermes-tui",
            "session-1",
            {"suggestion_id": "suggestion-1", "title": "继续推进"},
        )
        bridge.queue_resume_source("hermes-tui", "session-1", "suggestion-1")
        app = create_app(bridge=bridge)

        status, body = request(
            app,
            "GET",
            "/v1/bridge/events?platform=hermes-tui&session_id=session-1",
            None,
        )
        self.assertEqual(status, 200)
        self.assertEqual(body["cursor"], 1)
        self.assertEqual(body["events"][0]["event_type"], "suggestion.ready")

        status, body = request(
            app,
            "POST",
            "/v1/bridge/source/claim",
            {"platform": "hermes-tui", "session_id": "session-1"},
        )
        self.assertEqual(status, 200)
        self.assertEqual(body, {"source_suggestion_id": "suggestion-1"})

    def test_bridge_query_requires_platform_and_session(self) -> None:
        app = create_app(bridge=BridgeHub())

        status, body = request(app, "GET", "/v1/bridge/events", None)

        self.assertEqual(status, 400)
        self.assertEqual(body["error"], "invalid_bridge_query")


if __name__ == "__main__":
    unittest.main()
