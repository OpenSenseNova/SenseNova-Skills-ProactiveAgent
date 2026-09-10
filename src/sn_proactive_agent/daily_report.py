"""Daily project-progress reports for the Web dashboard.

The daily report is intentionally a small read model built from the durable
Markdown Project/Item files.  It does not introduce another source of truth
and it does not participate in the V1 turn contract.  Just after local
midnight, the service generates one closeout for the previous calendar date;
its lifecycle (generated, viewed, and dismissed) is recorded in
``runtime.jsonl`` so the Web client can safely resume after a restart.
"""

from __future__ import annotations

import threading
from datetime import date, datetime, time, timedelta
from typing import Any, Callable, Mapping

from .journal import RuntimeJournal, RuntimeRecord
from .storage import ItemState, MarkdownStore, ProjectMetadata, StorageError


class DailyReportError(RuntimeError):
    """Base class for daily-report failures."""


class DailyReportNotFoundError(DailyReportError):
    """Raised when an action targets a report that is not available."""


Clock = Callable[[], datetime]

_REPORT_KIND = "daily_report.generated"
_VIEWED_KIND = "daily_report.viewed"
_DISMISSED_KIND = "daily_report.dismissed"


class DailyReportService:
    """Generate and expose one concise closeout for the previous local date.

    ``clock`` is injectable so rollover and first-open behaviour can be tested
    without waiting for midnight.  The default clock uses the machine's local
    timezone, which is the date users see in the Web application.
    """

    def __init__(
        self,
        store: MarkdownStore,
        journal: RuntimeJournal,
        *,
        clock: Clock | None = None,
    ) -> None:
        self.store = store
        self.journal = journal
        self.clock = clock or (lambda: datetime.now().astimezone())
        self._lock = threading.RLock()
        self._scheduler_stop = threading.Event()
        self._scheduler_thread: threading.Thread | None = None

    def ensure_previous_day_report(self) -> dict[str, Any] | None:
        """Create the previous local day's report if needed.

        An empty workspace has no useful daily report.  We therefore defer the
        generated record until at least one readable Project exists; this
        avoids permanently recording an empty report when a service starts
        before the first Project is created.
        """

        with self._lock:
            # Read the clock once so a request that straddles midnight cannot
            # pair one date with another timestamp.
            now = self._now()
            # The report is a midnight closeout: at 00:xx on the 2nd, the
            # report date is the 1st.  This keeps the report stable for the
            # whole day while today's work is still in progress.
            report_date = now.date() - timedelta(days=1)
            existing = self._generated(report_date)
            if existing is None:
                projects = self._collect_projects()
                if not projects:
                    return None
                report = {
                    "report_id": _report_id(report_date),
                    "date": report_date.isoformat(),
                    "report_date": report_date.isoformat(),
                    "generated_at": _timestamp(now),
                    "projects": projects,
                }
                # The stable record id makes generation safe across concurrent
                # requests and service restarts.
                self.journal.append(
                    _REPORT_KIND,
                    f"generated-{report['report_id']}",
                    report,
                    recorded_at=now,
                )
                existing = report
            return self._with_lifecycle(existing)

    def ensure_current_report(self) -> dict[str, Any] | None:
        """Backward-compatible alias for the previous-day closeout."""

        return self.ensure_previous_day_report()

    def snapshot(self) -> dict[str, Any]:
        """Return the browser-facing report object.

        ``available`` is false, and ``report`` is null, until a Project exists.
        ``first_open_pending`` remains true until the client records either a
        view or a dismiss action; this lets the first Web open trigger a modal
        without treating a background polling request as a view.
        """

        report = self.ensure_previous_day_report()
        if report is None:
            return {
                "available": False,
                "report": None,
                "first_open_pending": False,
                "viewed_at": None,
                "dismissed_at": None,
                "status": "unavailable",
            }
        return self._public_snapshot(report)

    def mark_viewed(self, report_id: str | None = None) -> dict[str, Any]:
        """Record that the user opened a report and return its new snapshot."""

        return self._mark(_VIEWED_KIND, report_id)

    def mark_dismissed(self, report_id: str | None = None) -> dict[str, Any]:
        """Record that the user closed a report and return its new snapshot."""

        return self._mark(_DISMISSED_KIND, report_id)

    # Explicit aliases make the API intent readable to callers while keeping
    # the lifecycle implementation in one place.  ``close`` is reserved for
    # stopping the optional midnight scheduler below.
    view = mark_viewed
    dismiss = mark_dismissed

    def _mark(self, kind: str, requested_id: str | None) -> dict[str, Any]:
        with self._lock:
            report = self.ensure_previous_day_report()
            if report is None:
                raise DailyReportNotFoundError("no daily report is available")
            current_id = report["report_id"]
            if requested_id is not None:
                if not isinstance(requested_id, str) or not requested_id.strip():
                    raise ValueError("report_id must be a non-empty string")
                if requested_id != current_id:
                    raise DailyReportNotFoundError(
                        f"daily report not found: {requested_id}"
                    )
            now = self._now()
            # A stable action id is deliberately idempotent.  Repeated browser
            # retries cannot inflate the runtime journal.
            action = "viewed" if kind == _VIEWED_KIND else "dismissed"
            self.journal.append(
                kind,
                f"{action}-{current_id}",
                {
                    "report_id": current_id,
                    "date": report["date"],
                    f"{action}_at": _timestamp(now),
                },
                recorded_at=now,
            )
            updated = self.ensure_previous_day_report()
            assert updated is not None
            return self._public_snapshot(updated)

    def _public_snapshot(self, report: Mapping[str, Any]) -> dict[str, Any]:
        report_body = {
            key: value
            for key, value in report.items()
            if key
            not in {"first_open_pending", "viewed_at", "dismissed_at", "lifecycle_status"}
        }
        return {
            "available": True,
            "report": report_body,
            "first_open_pending": bool(report["first_open_pending"]),
            "viewed_at": report["viewed_at"],
            "dismissed_at": report["dismissed_at"],
            "status": report["lifecycle_status"],
        }

    def _generated(self, report_date: date) -> dict[str, Any] | None:
        expected_id = _report_id(report_date)
        for record in reversed(self.journal.records(_REPORT_KIND)):
            payload = dict(record.payload)
            if payload.get("report_id") != expected_id:
                continue
            payload_date = payload.get("date") or payload.get("report_date")
            if payload_date != report_date.isoformat():
                continue
            projects = payload.get("projects")
            if not isinstance(projects, list):
                raise DailyReportError(
                    f"daily report {expected_id} has invalid projects payload"
                )
            report = {
                "report_id": expected_id,
                "date": report_date.isoformat(),
                "report_date": report_date.isoformat(),
                "generated_at": payload.get("generated_at", record.recorded_at),
                "projects": projects,
            }
            return self._with_lifecycle(report)
        return None

    def _with_lifecycle(self, report: Mapping[str, Any]) -> dict[str, Any]:
        report_id = report["report_id"]
        viewed = self._latest_action(_VIEWED_KIND, report_id)
        dismissed = self._latest_action(_DISMISSED_KIND, report_id)
        viewed_at = _action_timestamp(viewed, "viewed_at")
        dismissed_at = _action_timestamp(dismissed, "dismissed_at")

        # Keep the latest action as the status for manual re-opening.  A
        # dismissed report remains available from the top-left history entry.
        if viewed is None and dismissed is None:
            status = "pending"
        elif dismissed is not None and (
            viewed is None or _record_is_at_or_after(dismissed, viewed)
        ):
            status = "dismissed"
        else:
            status = "viewed"

        result = dict(report)
        result.update(
            {
                "first_open_pending": viewed is None and dismissed is None,
                "viewed_at": viewed_at,
                "dismissed_at": dismissed_at,
                "lifecycle_status": status,
            }
        )
        return result

    def _latest_action(self, kind: str, report_id: str) -> RuntimeRecord | None:
        for record in reversed(self.journal.records(kind)):
            if record.payload.get("report_id") == report_id:
                return record
        return None

    def _collect_projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for metadata in self.store.list_projects():
            try:
                project = self.store.read_project(metadata.id)
            except StorageError:
                # A malformed or concurrently edited project should not make
                # the whole dashboard unavailable; the next report can pick
                # it up once the file is repaired.
                continue
            items: list[dict[str, Any]] = []
            for reference in project.items:
                try:
                    item = self.store.read_item(metadata.id, reference.id)
                except StorageError:
                    continue
                items.append(_item_projection(item))
            projects.append(_project_projection(metadata, items))
        return projects

    def _now(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise TypeError("daily report clock must return datetime")
        # An injected aware datetime is treated as already local.  Normalize a
        # naive test/host clock to the machine-local timezone so report dates
        # and lifecycle timestamps remain comparable across platforms.
        return value if value.tzinfo is not None else value.astimezone()

    def start(self) -> None:
        """Bootstrap the report and schedule a check at each local midnight.

        The scheduler is deliberately tiny and optional.  API requests still
        call :meth:`ensure_previous_day_report`, so environments that only
        construct a WSGI app (including tests and serverless hosts) get the
        same catch-up behaviour without a background thread.
        """

        with self._lock:
            self.ensure_previous_day_report()
            if self._scheduler_thread is not None and self._scheduler_thread.is_alive():
                return
            self._scheduler_stop.clear()
            self._scheduler_thread = threading.Thread(
                target=self._run_scheduler,
                name="sn-proactive-agent-daily-report",
                daemon=True,
            )
            self._scheduler_thread.start()

    def close(self) -> None:
        """Stop the optional midnight scheduler without touching report data."""

        with self._lock:
            thread = self._scheduler_thread
            self._scheduler_thread = None
            self._scheduler_stop.set()
        if thread is not None:
            thread.join(timeout=2)

    def _run_scheduler(self) -> None:
        while not self._scheduler_stop.is_set():
            wait_seconds = self._seconds_until_midnight()
            if self._scheduler_stop.wait(wait_seconds):
                return
            try:
                self.ensure_previous_day_report()
            except Exception:
                # A malformed project should not kill the service's scheduler;
                # the next Web request will retry and surface the normal API
                # error if the workspace remains unusable.
                continue

    def _seconds_until_midnight(self) -> float:
        now = self._now()
        tz = now.tzinfo
        next_date = now.date() + timedelta(days=1)
        if tz is None:
            next_midnight = datetime.combine(next_date, time.min)
        else:
            next_midnight = datetime.combine(next_date, time.min, tzinfo=tz)
        return max(0.1, (next_midnight - now).total_seconds())


def _report_id(report_date: date) -> str:
    return f"daily-report-{report_date.isoformat()}"


def _timestamp(value: datetime) -> str:
    rendered = value.isoformat()
    return rendered.replace("+00:00", "Z")


def _action_timestamp(record: RuntimeRecord | None, field: str) -> str | None:
    if record is None:
        return None
    value = record.payload.get(field)
    if isinstance(value, str) and value.strip():
        return value
    return record.recorded_at


def _record_is_at_or_after(left: RuntimeRecord, right: RuntimeRecord) -> bool:
    """Compare runtime timestamps without relying on offset string ordering."""

    try:
        left_at = datetime.fromisoformat(left.recorded_at.replace("Z", "+00:00"))
        right_at = datetime.fromisoformat(right.recorded_at.replace("Z", "+00:00"))
        return left_at >= right_at
    except (TypeError, ValueError):
        # RuntimeJournal validates non-empty strings, but old records may use
        # a non-ISO timestamp.  Preserve deterministic append-order fallback.
        return left.recorded_at >= right.recorded_at


def _item_projection(item: ItemState) -> dict[str, Any]:
    return {
        "item_id": item.id,
        "id": item.id,
        "name": item.name,
        "status": item.status,
        "current_progress": _compact(item.current_progress),
        "next_step": _compact(item.next_step),
        "blocker": _compact(item.blocker) if item.blocker else None,
    }


def _project_projection(
    metadata: ProjectMetadata,
    items: list[dict[str, Any]],
) -> dict[str, Any]:
    statuses = [item["status"] for item in items]
    completed = statuses.count("completed")
    active = statuses.count("in_progress")
    blocked = statuses.count("blocked")
    planned = statuses.count("planned")

    if not items:
        concise = _compact(metadata.summary)
    else:
        counts: list[str] = []
        if completed:
            counts.append(f"已完成 {completed} 项")
        if active:
            counts.append(f"进行中 {active} 项")
        if blocked:
            counts.append(f"阻塞 {blocked} 项")
        if planned:
            counts.append(f"待开始 {planned} 项")
        highlights = "；".join(
            f"{item['name']}：{item['current_progress']}"
            for item in items[:2]
        )
        concise = "，".join(counts)
        if highlights:
            concise = f"{concise}。{_compact(highlights)}"

    if not items:
        next_step = "暂无待处理事项。"
    else:
        pending = [item for item in items if item["status"] != "completed"]
        next_step = (
            _compact(pending[0]["next_step"]) if pending else "所有事项已完成。"
        )
    return {
        "project_id": metadata.id,
        "id": metadata.id,
        "name": metadata.name,
        "status": metadata.status,
        "summary": concise,
        "project_summary": _compact(metadata.summary),
        "next_step": next_step,
        "item_count": len(items),
        "items": items,
    }


def _compact(value: str, limit: int = 160) -> str:
    normalized = " ".join(str(value).split())
    if len(normalized) <= limit:
        return normalized
    return normalized[: max(1, limit - 1)].rstrip() + "…"
