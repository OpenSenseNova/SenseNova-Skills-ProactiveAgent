from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any
from wsgiref.util import setup_testing_defaults

from sn_proactive_agent.api import create_app
from sn_proactive_agent.daily_report import DailyReportService
from sn_proactive_agent.journal import RuntimeJournal
from sn_proactive_agent.storage import ItemState, MarkdownStore, ProjectMetadata


def request(
    app: Any,
    method: str,
    path: str,
    payload: dict[str, Any] | None = None,
) -> tuple[int, dict[str, Any]]:
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
    return int(captured["status"].split(" ", 1)[0]), json.loads(response_body)


class MutableClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def __call__(self) -> datetime:
        return self.value


class DailyReportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary_directory.cleanup)
        self.data_root = Path(self.temporary_directory.name) / "data"
        self.store = MarkdownStore(self.data_root)
        self.journal = RuntimeJournal(self.data_root)
        self.store.create_project(
            ProjectMetadata(
                id="project-001",
                name="客户数据看板",
                summary="跟踪数据分析与客户汇报材料。",
            )
        )
        self.store.create_item(
            "project-001",
            ItemState(
                id="item-001",
                name="本周分析",
                status="in_progress",
                goal="完成本周数据分析。",
                completion_criteria="结论已经确认。",
                current_progress="留存数据已跑完，正在整理关键变化。",
                next_step="用新结论更新周报。",
            ),
        )
        self.clock = MutableClock(
            datetime(2026, 9, 2, 1, 0, tzinfo=timezone.utc)
        )
        self.reports = DailyReportService(
            self.store,
            self.journal,
            clock=self.clock,
        )

    def test_generates_one_previous_day_closeout_per_local_date(self) -> None:
        first = self.reports.snapshot()
        second = self.reports.snapshot()

        self.assertTrue(first["available"])
        self.assertTrue(first["first_open_pending"])
        self.assertEqual(first["report"]["date"], "2026-09-01")
        self.assertEqual(first["report"]["report_date"], "2026-09-01")
        self.assertEqual(first["report"]["report_id"], "daily-report-2026-09-01")
        project = first["report"]["projects"][0]
        self.assertEqual(project["project_id"], "project-001")
        self.assertEqual(project["item_count"], 1)
        self.assertIn("进行中 1 项", project["summary"])
        self.assertIn("留存数据已跑完", project["summary"])
        self.assertEqual(second["report"]["report_id"], first["report"]["report_id"])
        self.assertEqual(len(self.journal.records("daily_report.generated")), 1)

    def test_rollover_generates_the_next_date_and_keeps_previous_record(self) -> None:
        self.reports.ensure_current_report()
        self.clock.value = datetime(2026, 9, 4, 0, 1, tzinfo=timezone.utc)

        report = self.reports.snapshot()

        self.assertEqual(report["report"]["date"], "2026-09-03")
        self.assertEqual(len(self.journal.records("daily_report.generated")), 2)

    def test_view_and_dismiss_are_idempotent_and_auditable(self) -> None:
        initial = self.reports.snapshot()
        report_id = initial["report"]["report_id"]

        viewed = self.reports.mark_viewed(report_id)
        viewed_again = self.reports.mark_viewed(report_id)
        self.assertFalse(viewed["first_open_pending"])
        self.assertEqual(viewed["status"], "viewed")
        self.assertEqual(viewed_again["viewed_at"], viewed["viewed_at"])
        self.assertEqual(len(self.journal.records("daily_report.viewed")), 1)

        dismissed = self.reports.mark_dismissed(report_id)
        self.assertEqual(dismissed["status"], "dismissed")
        self.assertIsNotNone(dismissed["dismissed_at"])
        self.assertEqual(len(self.journal.records("daily_report.dismissed")), 1)

    def test_empty_workspace_defers_generation_until_a_project_exists(self) -> None:
        empty_root = Path(self.temporary_directory.name) / "empty"
        empty_store = MarkdownStore(empty_root)
        empty_journal = RuntimeJournal(empty_root)
        reports = DailyReportService(empty_store, empty_journal, clock=self.clock)

        self.assertFalse(reports.snapshot()["available"])
        self.assertEqual(empty_journal.records("daily_report.generated"), ())

    def test_project_without_items_has_an_honest_next_step(self) -> None:
        root = Path(self.temporary_directory.name) / "no-items"
        store = MarkdownStore(root)
        journal = RuntimeJournal(root)
        store.create_project(ProjectMetadata("project-empty", "资料整理", "整理待确认资料。"))
        report = DailyReportService(store, journal, clock=self.clock).snapshot()

        self.assertEqual(
            report["report"]["projects"][0]["next_step"],
            "暂无待处理事项。",
        )

    def test_http_daily_report_get_view_and_dismiss(self) -> None:
        app = create_app(store=self.store, journal=self.journal, daily_reports=self.reports)
        status, payload = request(app, "GET", "/api/daily-report")
        self.assertEqual(status, 200)
        self.assertEqual(payload["report"]["date"], "2026-09-01")
        report_id = payload["report"]["report_id"]

        status, payload = request(
            app,
            "POST",
            "/api/daily-report/view",
            {"report_id": report_id},
        )
        self.assertEqual(status, 200)
        self.assertFalse(payload["first_open_pending"])

        status, payload = request(
            app,
            "POST",
            "/api/daily-report/dismiss",
            {"report_id": report_id},
        )
        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "dismissed")

    def test_http_actions_allow_an_empty_json_body(self) -> None:
        app = create_app(store=self.store, journal=self.journal, daily_reports=self.reports)

        status, payload = request(app, "POST", "/api/daily-report/view")

        self.assertEqual(status, 200)
        self.assertEqual(payload["status"], "viewed")

    def test_dashboard_includes_daily_report_projection(self) -> None:
        app = create_app(store=self.store, journal=self.journal, daily_reports=self.reports)
        status, payload = request(app, "GET", "/api/dashboard")

        self.assertEqual(status, 200)
        self.assertIn("daily_report", payload)
        self.assertEqual(payload["daily_report"]["report"]["report_id"], "daily-report-2026-09-01")

    def test_lifecycle_survives_service_restart(self) -> None:
        report_id = self.reports.snapshot()["report"]["report_id"]
        self.reports.mark_dismissed(report_id)

        restarted = DailyReportService(self.store, self.journal, clock=self.clock)
        snapshot = restarted.snapshot()

        self.assertEqual(snapshot["report"]["report_id"], report_id)
        self.assertFalse(snapshot["first_open_pending"])
        self.assertEqual(snapshot["status"], "dismissed")

    def test_midnight_scheduler_can_start_and_stop(self) -> None:
        self.reports.start()
        try:
            thread = self.reports._scheduler_thread
            self.assertIsNotNone(thread)
            self.assertTrue(thread.is_alive())
        finally:
            self.reports.close()

        self.assertIsNone(self.reports._scheduler_thread)


if __name__ == "__main__":
    unittest.main()
