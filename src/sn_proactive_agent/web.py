"""Web dashboard projection for the Proactive Agent service.

The dashboard is deliberately a read model.  Markdown Project/Item files and
the JSONL runtime journal remain the durable sources of truth; this module
only turns them into small JSON objects that are convenient for a browser to
render.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone
from typing import Any

from .daily_report import DailyReportService
from .journal import RuntimeJournal, RuntimeRecord
from .storage import MarkdownStore, StorageError


_EVENT_BLOCK_RE = re.compile(
    r"<!-- event:start (?P<id>[a-z0-9][a-z0-9._-]{0,191}) -->\s*"
    r"(?P<body>.*?)"
    r"<!-- event:end (?P=id) -->",
    re.DOTALL,
)
_YAML_FENCE_RE = re.compile(r"```yaml\s*(?P<body>.*?)\s*```", re.DOTALL)
_SECTION_RE = re.compile(
    r"^### (?P<title>[^\n]+)\s*$\n(?P<body>.*?)(?=^### |\Z)",
    re.MULTILINE | re.DOTALL,
)


class DashboardProjection:
    """Build the browser-facing dashboard JSON from durable local files."""

    def __init__(
        self,
        store: MarkdownStore,
        journal: RuntimeJournal,
        *,
        daily_reports: DailyReportService | None = None,
    ) -> None:
        self.store = store
        self.journal = journal
        self.daily_reports = daily_reports

    def snapshot(self) -> dict[str, Any]:
        snapshot: dict[str, Any] = {
            "generated_at": _now(),
            "suggestions": self._suggestions(),
            "latest_decision": self._latest_decision(),
            "projects": self._projects(),
        }
        if self.daily_reports is not None:
            # The daily brief is an auxiliary read model.  If its persisted
            # record is temporarily malformed or unavailable, keep the main
            # suggestions and Project/Item dashboard usable and retry later.
            try:
                snapshot["daily_report"] = self.daily_reports.snapshot()
            except Exception:
                snapshot["daily_report"] = {
                    "available": False,
                    "report": None,
                    "first_open_pending": False,
                    "viewed_at": None,
                    "dismissed_at": None,
                    "status": "error",
                }
        return snapshot

    def project(self, project_id: str) -> dict[str, Any]:
        for project in self._projects():
            if project["id"] == project_id:
                return project
        raise KeyError(project_id)

    def events(
        self,
        project_id: str,
        item_id: str,
        *,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("limit must be between 1 and 100")
        text = self.store.read_events(project_id, item_id)
        parsed = [_parse_event(match.group("id"), match.group("body")) for match in _EVENT_BLOCK_RE.finditer(text)]
        return list(reversed(parsed[-limit:]))

    def _projects(self) -> list[dict[str, Any]]:
        projects: list[dict[str, Any]] = []
        for metadata in self.store.list_projects():
            try:
                record = self.store.read_project(metadata.id)
            except StorageError:
                continue
            items: list[dict[str, Any]] = []
            for reference in record.items:
                try:
                    item = self.store.read_item(metadata.id, reference.id)
                    events_text = self.store.read_events(metadata.id, reference.id)
                except StorageError:
                    continue
                events = list(_EVENT_BLOCK_RE.finditer(events_text))
                last_event = (
                    _parse_event(events[-1].group("id"), events[-1].group("body"))
                    if events
                    else None
                )
                items.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "status": item.status,
                        "goal": item.goal,
                        "completion_criteria": item.completion_criteria,
                        "current_progress": item.current_progress,
                        "next_step": item.next_step,
                        "blocker": item.blocker,
                        "event_count": len(events),
                        "last_event": last_event,
                    }
                )
            projects.append(
                {
                    "id": metadata.id,
                    "name": metadata.name,
                    "summary": metadata.summary,
                    "status": metadata.status,
                    "items": items,
                    "item_count": len(items),
                    "completed_item_count": sum(
                        item["status"] == "completed" for item in items
                    ),
                }
            )
        return projects

    def _suggestions(self) -> list[dict[str, Any]]:
        ready_records = self.journal.records("suggestion.ready")
        responses = _latest_by_payload_key(
            self.journal.records("suggestion.responded"), "suggestion_id"
        )
        executed = _latest_by_payload_key(
            self.journal.records("suggestion.executed"), "suggestion_id"
        )
        failures = _latest_by_payload_key(
            self.journal.records("session.resume.failed"), "suggestion_id"
        )
        requests = _latest_by_payload_key(
            self.journal.records("session.resume.requested"), "suggestion_id"
        )

        suggestions: list[dict[str, Any]] = []
        seen: set[str] = set()
        for record in reversed(ready_records):
            suggestion_id = _payload_text(record, "suggestion_id")
            if suggestion_id is None or suggestion_id in seen:
                continue
            seen.add(suggestion_id)
            payload = dict(record.payload)
            response = responses.get(suggestion_id)
            executed_record = executed.get(suggestion_id)
            failure = failures.get(suggestion_id)
            request = requests.get(suggestion_id)
            if failure is not None:
                status = "failed"
            elif executed_record is not None:
                status = "completed"
            elif response is not None and response.payload.get("choice") == "ignore":
                status = "ignored"
            elif response is not None:
                status = "resuming" if request is not None else "approved"
            else:
                status = "pending"
            suggestions.append(
                {
                    "suggestion_id": suggestion_id,
                    "platform": payload.get("platform"),
                    "created_at": record.recorded_at,
                    "target_session_id": payload.get("target_session_id"),
                    "title": payload.get("title", "建议继续推进"),
                    "why_now": payload.get("why_now", ""),
                    "evidence": payload.get("evidence", ""),
                    "suggested_action": payload.get("suggested_action", ""),
                    "status": status,
                    "response": _record_payload(response),
                    "resume": _record_payload(request),
                    "failure": _record_payload(failure),
                    "executed": _record_payload(executed_record),
                }
            )
        return suggestions

    def _latest_decision(self) -> dict[str, Any] | None:
        records = self.journal.records("judge.decision")
        if not records:
            return None
        record = records[-1]
        payload = dict(record.payload)
        return {
            "recorded_at": record.recorded_at,
            "outcome": payload.get("outcome"),
            "reason": payload.get("reason", ""),
            "event_id": payload.get("event_id"),
            "project_id": payload.get("project_id"),
            "item_id": payload.get("item_id"),
            "suggestion_id": payload.get("suggestion_id"),
        }


def _latest_by_payload_key(
    records: tuple[RuntimeRecord, ...],
    field: str,
) -> dict[str, RuntimeRecord]:
    result: dict[str, RuntimeRecord] = {}
    for record in records:
        value = record.payload.get(field)
        if isinstance(value, str) and value.strip():
            result[value] = record
    return result


def _record_payload(record: RuntimeRecord | None) -> dict[str, Any] | None:
    if record is None:
        return None
    return {
        "recorded_at": record.recorded_at,
        "payload": dict(record.payload),
    }


def _payload_text(record: RuntimeRecord, field: str) -> str | None:
    value = record.payload.get(field)
    return value.strip() if isinstance(value, str) and value.strip() else None


def _parse_event(event_id: str, body: str) -> dict[str, Any]:
    metadata_match = _YAML_FENCE_RE.search(body)
    metadata = _parse_metadata(metadata_match.group("body") if metadata_match else "")
    sections = {
        match.group("title").strip(): match.group("body").strip()
        for match in _SECTION_RE.finditer(body)
    }
    source = metadata.get("source")
    source = source if isinstance(source, dict) else {}
    return {
        "id": event_id,
        "completed_at": metadata.get("completed_at"),
        "summary": metadata.get("summary", ""),
        "source": source,
        "source_suggestion_id": metadata.get("source_suggestion_id"),
        "updates": metadata.get("updates", {}),
        "question": sections.get("原始问题", ""),
        "answer": sections.get("原始回答", ""),
    }


def _parse_metadata(text: str) -> dict[str, Any]:
    result: dict[str, Any] = {}
    section: dict[str, Any] | None = None
    for raw_line in text.splitlines():
        if not raw_line.strip():
            continue
        indentation = len(raw_line) - len(raw_line.lstrip())
        stripped = raw_line.strip()
        if ":" not in stripped:
            continue
        key, raw_value = stripped.split(":", 1)
        key = key.strip()
        value = _yaml_value(raw_value.strip())
        if indentation and section is not None:
            section[key] = value
            continue
        if raw_value.strip() == "":
            section = {}
            result[key] = section
        else:
            result[key] = value
            section = None
    return result


def _yaml_value(value: str) -> Any:
    if value in {"null", "~"}:
        return None
    if len(value) >= 2 and value[0] == value[-1] and value[0] in {'"', "'"}:
        return value[1:-1]
    return value


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
