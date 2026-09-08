from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

from proactive_memory_service.api import create_app
from proactive_memory_service.contracts import SuggestionReady
from proactive_memory_service.journal import RuntimeJournal
from proactive_memory_service.storage import ItemState, MarkdownStore, ProjectMetadata


def request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, list[tuple[str, str]], bytes]:
    body = b"" if payload is None else json.dumps(payload).encode("utf-8")
    route, separator, query_string = path.partition("?")
    environ: dict[str, Any] = {}
    setup_testing_defaults(environ)
    environ.update(
        {
            "REQUEST_METHOD": method,
            "PATH_INFO": route,
            "QUERY_STRING": query_string if separator else "",
            "CONTENT_TYPE": "application/json",
            "CONTENT_LENGTH": str(len(body)),
            "wsgi.input": BytesIO(body),
        }
    )
    captured: dict[str, Any] = {}

    def start_response(status: str, headers: list[tuple[str, str]]) -> None:
        captured["status"] = status
        captured["headers"] = headers

    response_body = b"".join(app(environ, start_response))
    return (
        int(captured["status"].split(" ", 1)[0]),
        captured["headers"],
        response_body,
    )


class WebDashboardTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.store.create_project(
            ProjectMetadata(
                id="project-001",
                name="发布页交付",
                summary="跟踪发布页从材料确认到上线。",
            )
        )
        self.store.create_item(
            "project-001",
            ItemState(
                id="item-001",
                name="前端制作",
                status="blocked",
                goal="完成发布页前端制作。",
                completion_criteria="正式页面通过验收。",
                current_progress="等待客户材料。",
                next_step="材料齐备后制定开工计划。",
                blocker="Logo 和品牌色尚未确认。",
            ),
        )
        self.app = create_app(store=self.store, journal=self.journal)

    def test_root_serves_the_dashboard_shell(self) -> None:
        status, headers, body = request(self.app, "GET", "/")

        self.assertEqual(status, 200)
        self.assertIn(("Content-Type", "text/html; charset=utf-8"), headers)
        html = body.decode("utf-8")
        self.assertIn("建议与决策", html)
        self.assertIn("项目进展", html)
        self.assertNotIn("先处理建议，再看项目进度", html)
        self.assertNotIn("可见的闭环", html)
        self.assertIn("/static/styles.css", html)
        self.assertIn("日报", html)
        self.assertIn('id="daily-report-modal"', html)
        self.assertIn('id="daily-report-open"', html)

    def test_daily_report_assets_expose_first_open_modal_behaviour(self) -> None:
        status, _headers, body = request(self.app, "GET", "/static/app.js")

        self.assertEqual(status, 200)
        script = body.decode("utf-8")
        self.assertIn("/api/daily-report", script)
        self.assertIn("/api/daily-report/${action}", script)
        self.assertIn("first_open_pending", script)
        self.assertIn("localStorage", script)
        self.assertIn("openDailyReport", script)
        self.assertIn("expandedProjects", script)
        self.assertIn("toggleProject", script)
        self.assertIn("data-project-toggle", script)
        self.assertIn("renderItemProgressBar", script)
        self.assertIn("stageByStatus", script)
        self.assertIn("未开始", script)
        self.assertIn("计划中", script)
        self.assertIn("进行中", script)
        self.assertIn("已完成", script)
        self.assertIn("return response.ok", script)

        status, _headers, body = request(self.app, "GET", "/static/styles.css")

        self.assertEqual(status, 200)
        styles = body.decode("utf-8")
        self.assertIn(".report-modal", styles)
        self.assertIn(".report-trigger", styles)
        self.assertIn(".items-list.open", styles)
        self.assertIn(".project-toggle", styles)
        self.assertIn(".item-progress-track", styles)
        self.assertIn(".item-progress-segment-blocked", styles)
        self.assertIn(".item-progress-widget", styles)
        self.assertIn(".item-card-main", styles)
        self.assertIn("-webkit-line-clamp: 2", styles)
        self.assertIn("-webkit-line-clamp: 3", styles)
        self.assertIn('class=\"project-summary\" title=', script)
        self.assertIn('class=\"item-progress\" title=', script)

    def test_dashboard_projects_are_projected_from_markdown(self) -> None:
        suggestion = SuggestionReady(
            suggestion_id="suggestion-001",
            platform="hermes-acp",
            created_at=datetime(2026, 8, 31, 10, 0, tzinfo=timezone.utc),
            target_session_id="session-001",
            title="制定前端开工计划",
            why_now="客户材料已经齐备。",
            evidence="Item blocker 已清除。",
            suggested_action="请制定前端开工任务单。",
        )
        self.journal.append(
            "judge.decision",
            "decision-001",
            {
                "event_id": "event-001",
                "outcome": "suggest",
                "reason": suggestion.why_now,
                "project_id": "project-001",
                "item_id": "item-001",
                "suggestion_id": suggestion.suggestion_id,
            },
        )
        self.journal.append(
            "suggestion.ready",
            suggestion.suggestion_id,
            suggestion.to_payload(),
            recorded_at=suggestion.created_at,
        )

        status, _headers, body = request(self.app, "GET", "/api/dashboard")

        self.assertEqual(status, 200)
        dashboard = json.loads(body.decode("utf-8"))
        self.assertEqual(dashboard["suggestions"][0]["status"], "pending")
        self.assertEqual(dashboard["suggestions"][0]["title"], "制定前端开工计划")
        # The browser uses only suggested_action for the compact card, while
        # the full rationale remains available to audit/debug consumers.
        self.assertEqual(
            dashboard["suggestions"][0]["why_now"],
            "客户材料已经齐备。",
        )
        self.assertEqual(
            dashboard["suggestions"][0]["evidence"],
            "Item blocker 已清除。",
        )
        self.assertEqual(
            dashboard["suggestions"][0]["suggested_action"],
            "请制定前端开工任务单。",
        )
        self.assertEqual(dashboard["projects"][0]["name"], "发布页交付")
        item = dashboard["projects"][0]["items"][0]
        self.assertEqual(item["status"], "blocked")
        self.assertEqual(item["blocker"], "Logo 和品牌色尚未确认。")

    def test_item_events_are_returned_as_collapsed_detail_data(self) -> None:
        events = self.store.read_events("project-001", "item-001")
        event_block = events.replace(
            "# Events\n",
            """
<!-- event:start event-001 -->

## event-001

```yaml
id: event-001
completed_at: 2026-08-31T10:00:00Z
source:
  platform: hermes-acp
  session_id: session-001
  turn_id: turn-001
source_suggestion_id: null
summary: 客户材料仍未齐备。
updates:
  blocker: Logo 和品牌色尚未确认。
```

### 原始问题

材料还没有确认。

### 原始回答

已记录阻塞。

<!-- event:end event-001 -->
""",
        )
        (self.data_root / "projects/project-001/items/item-001/events.md").write_text(
            event_block,
            encoding="utf-8",
        )

        status, _headers, body = request(
            self.app,
            "GET",
            "/api/projects/project-001/items/item-001/events?limit=5",
        )

        self.assertEqual(status, 200)
        payload = json.loads(body.decode("utf-8"))
        self.assertEqual(payload["events"][0]["id"], "event-001")
        self.assertEqual(payload["events"][0]["source"]["platform"], "hermes-acp")
        self.assertEqual(payload["events"][0]["question"], "材料还没有确认。")
        self.assertEqual(payload["events"][0]["answer"], "已记录阻塞。")

    def test_dashboard_requires_storage_configuration(self) -> None:
        app = create_app()

        status, _headers, body = request(app, "GET", "/api/dashboard")

        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "dashboard_not_configured")

    def test_daily_report_failure_does_not_hide_dashboard(self) -> None:
        class BrokenDailyReport:
            def ensure_previous_day_report(self) -> None:
                raise RuntimeError("temporary report failure")

            def snapshot(self) -> dict[str, object]:
                raise RuntimeError("temporary report failure")

        app = create_app(
            store=self.store,
            journal=self.journal,
            daily_reports=BrokenDailyReport(),  # type: ignore[arg-type]
        )

        status, _headers, body = request(app, "GET", "/api/dashboard")
        self.assertEqual(status, 200)
        dashboard = json.loads(body.decode("utf-8"))
        self.assertEqual(dashboard["projects"][0]["name"], "发布页交付")
        self.assertEqual(dashboard["daily_report"]["status"], "error")

        status, _headers, body = request(app, "GET", "/api/daily-report")
        self.assertEqual(status, 503)
        self.assertEqual(json.loads(body.decode("utf-8"))["error"], "daily_report_unavailable")


if __name__ == "__main__":
    unittest.main()
