"""Minimal WSGI boundary for inbound V1 Proactive Memory events."""

from __future__ import annotations

import json
from http import HTTPStatus
from pathlib import Path
from typing import Any, Iterable, Protocol
from urllib.parse import parse_qs

from .bridge import BridgeHub
from .contracts import (
    ContractValidationError,
    EventType,
    InboundEvent,
    SessionResumeFailed,
    TurnCompleted,
    TurnStarted,
    parse_event,
)
from .daily_report import DailyReportError, DailyReportNotFoundError, DailyReportService
from .journal import RuntimeJournal
from .storage import MarkdownStore, StorageError
from .web import DashboardProjection


class InboundEventHandler(Protocol):
    def handle(self, event: InboundEvent) -> None:
        """Handle one already validated inbound event."""


class NullEventHandler:
    def handle(self, event: InboundEvent) -> None:
        del event


_INBOUND_ROUTES = {
    f"/v1/events/{EventType.TURN_STARTED.value}": EventType.TURN_STARTED,
    f"/v1/events/{EventType.TURN_COMPLETED.value}": EventType.TURN_COMPLETED,
    f"/v1/events/{EventType.SUGGESTION_RESPONDED.value}": EventType.SUGGESTION_RESPONDED,
    f"/v1/events/{EventType.SESSION_RESUME_FAILED.value}": EventType.SESSION_RESUME_FAILED,
}


class EventApplication:
    """Small WSGI application that validates the V1 inbound contract."""

    def __init__(
        self,
        handler: InboundEventHandler | None = None,
        *,
        bridge: BridgeHub | None = None,
        store: MarkdownStore | None = None,
        journal: RuntimeJournal | None = None,
        daily_reports: DailyReportService | None = None,
    ) -> None:
        self._handler = handler or NullEventHandler()
        self._bridge = bridge
        self._daily_reports = daily_reports
        if self._daily_reports is None and store is not None and journal is not None:
            self._daily_reports = DailyReportService(store, journal)
        self._dashboard = (
            DashboardProjection(store, journal, daily_reports=self._daily_reports)
            if store is not None and journal is not None
            else None
        )
        self._web_root = Path(__file__).resolve().parent / "web"
        # This is intentionally a best-effort bootstrap.  The same check is
        # repeated on every report/dashboard request, so a service that starts
        # before its first Project is created will still generate a report
        # later in the day.
        if self._daily_reports is not None:
            # Report generation is auxiliary to the event/API boundary.  A
            # bad report record should be retried later instead of preventing
            # health and dashboard routes from starting.
            try:
                self._daily_reports.ensure_previous_day_report()
            except Exception:
                pass

    def __call__(self, environ: dict[str, Any], start_response: Any) -> Iterable[bytes]:
        method = environ.get("REQUEST_METHOD", "GET").upper()
        path = environ.get("PATH_INFO", "")

        if path == "/health":
            if method != "GET":
                return self._respond(
                    start_response,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed"},
                    headers=[("Allow", "GET")],
                )
            return self._respond(start_response, HTTPStatus.OK, {"status": "ok"})

        if path == "/":
            if method != "GET":
                return self._respond(
                    start_response,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed"},
                    headers=[("Allow", "GET")],
                )
            return self._serve_asset(start_response, "index.html", "text/html; charset=utf-8")

        if path.startswith("/static/"):
            if method != "GET":
                return self._respond(
                    start_response,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed"},
                    headers=[("Allow", "GET")],
                )
            asset = path.removeprefix("/static/")
            content_type = {
                "app.js": "text/javascript; charset=utf-8",
                "styles.css": "text/css; charset=utf-8",
            }.get(asset)
            if content_type is None or "/" in asset or "\\" in asset:
                return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
            return self._serve_asset(start_response, asset, content_type)

        if path == "/api/dashboard":
            if method != "GET":
                return self._respond(
                    start_response,
                    HTTPStatus.METHOD_NOT_ALLOWED,
                    {"error": "method_not_allowed"},
                    headers=[("Allow", "GET")],
                )
            if self._dashboard is None:
                return self._respond(
                    start_response,
                    HTTPStatus.SERVICE_UNAVAILABLE,
                    {"error": "dashboard_not_configured"},
                )
            return self._respond(start_response, HTTPStatus.OK, self._dashboard.snapshot())

        if path == "/api/daily-report":
            return self._daily_report_get(start_response, method)

        if path in {
            "/api/daily-report/view",
            "/api/daily-report/dismiss",
        }:
            action = "view" if path.endswith("/view") else "dismiss"
            return self._daily_report_action(environ, start_response, method, action)

        if path.startswith("/api/projects/") and path.endswith("/events"):
            return self._project_events(environ, start_response, method, path)

        if path == "/v1/bridge/events":
            return self._bridge_events(environ, start_response, method)

        if path == "/v1/bridge/source/claim":
            return self._bridge_source_claim(environ, start_response, method)

        if path in {"/v1/bridge/heartbeat", "/v1/bridge/resume/claim", "/v1/bridge/diagnostics"}:
            return self._bridge_diagnostics(environ, start_response, method, path)

        event_type = _INBOUND_ROUTES.get(path)
        if event_type is None:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                {"error": "not_found"},
            )
        if method != "POST":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "POST")],
            )

        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return self._respond(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "content_type_must_be_application_json"},
            )

        try:
            payload = self._read_json(environ)
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_json"},
            )

        try:
            event = parse_event(event_type, payload)
        except ContractValidationError as exc:
            return self._respond(
                start_response,
                HTTPStatus.UNPROCESSABLE_ENTITY,
                {"error": "invalid_event", "detail": str(exc)},
            )

        self._handler.handle(event)
        if self._bridge is not None and isinstance(event, (TurnStarted, TurnCompleted)):
            self._bridge.observe_turn(event)
        elif self._bridge is not None and isinstance(event, SessionResumeFailed):
            self._bridge.cancel_resume(event.suggestion_id)
        return self._respond(
            start_response,
            HTTPStatus.ACCEPTED,
            {"accepted": True, "event_type": event_type.value},
        )

    def _bridge_diagnostics(self, environ, start_response, method, path):
        if self._bridge is None:
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        expected_method = "GET" if path.endswith("/diagnostics") else "POST"
        if method != expected_method:
            return self._respond(start_response, HTTPStatus.METHOD_NOT_ALLOWED,
                                 {"error": "method_not_allowed"}, headers=[("Allow", expected_method)])
        try:
            if method == "GET":
                query = parse_qs(environ.get("QUERY_STRING", ""))
                payload = {key: _query_text(query, key) for key in ("platform", "session_id", "instance_id")}
            else:
                if environ.get("CONTENT_TYPE", "").split(";", 1)[0] != "application/json":
                    return self._respond(start_response, HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                                         {"error": "content_type_must_be_application_json"})
                if not 0 < int(environ.get("CONTENT_LENGTH", "0")) <= 8192:
                    raise ValueError("invalid body length")
                payload = self._read_json(environ)
            if not isinstance(payload, dict):
                raise ValueError("object required")
            fields = ["platform", "session_id", "instance_id"]
            if method == "POST":
                fields.append("client_id")
            if path.endswith("/resume/claim"):
                fields.append("suggestion_id")
            if any(not isinstance(payload.get(key), str) or not 0 < len(payload[key]) <= 256 for key in fields):
                raise ValueError("invalid bridge identifier")
            args = {key: payload[key] for key in fields}
            if path.endswith("/heartbeat"):
                probe, ready = payload.get("probe", ""), payload.get("ready", False)
                if not isinstance(probe, str) or len(probe) > 256 or not isinstance(ready, bool):
                    raise ValueError("invalid probe state")
                result = self._bridge.heartbeat(**args, probe=probe, ready=ready)
            elif path.endswith("/resume/claim"):
                result = self._bridge.claim_resume(**args)
            else:
                result = self._bridge.diagnostics(**args)
        except (ValueError, TypeError, KeyError, UnicodeDecodeError):
            return self._respond(start_response, HTTPStatus.BAD_REQUEST, {"error": "invalid_bridge_request"})
        return self._respond(start_response, HTTPStatus.OK, result)

    def _bridge_events(
        self,
        environ: dict[str, Any],
        start_response: Any,
        method: str,
    ) -> Iterable[bytes]:
        if self._bridge is None:
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        if method != "GET":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "GET")],
            )
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        try:
            platform = _query_text(query, "platform")
            session_id = _query_text(query, "session_id")
            after = _query_int(query, "after", default=0, minimum=0)
            timeout = _query_float(query, "timeout", default=0.0, minimum=0.0, maximum=25.0)
        except ValueError as exc:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_bridge_query", "detail": str(exc)},
            )
        cursor, records = self._bridge.poll(
            platform,
            session_id,
            after=after,
            timeout_seconds=timeout,
        )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            {
                "cursor": cursor,
                "events": [record.to_payload() for record in records],
            },
        )

    def _project_events(
        self,
        environ: dict[str, Any],
        start_response: Any,
        method: str,
        path: str,
    ) -> Iterable[bytes]:
        if method != "GET":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "GET")],
            )
        if self._dashboard is None:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "dashboard_not_configured"},
            )
        parts = path.strip("/").split("/")
        if (
            len(parts) != 6
            or parts[0:2] != ["api", "projects"]
            or parts[3] != "items"
            or parts[5] != "events"
        ):
            # The route matcher above is intentionally conservative.  This
            # branch keeps malformed paths from becoming filesystem lookups.
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        project_id, item_id = parts[2], parts[4]
        # The canonical route is /api/projects/<project>/items/<item>/events;
        # split it explicitly so IDs remain opaque to the presentation layer.
        if not project_id or not item_id:
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        query = parse_qs(environ.get("QUERY_STRING", ""), keep_blank_values=True)
        try:
            limit = _query_int(query, "limit", default=20, minimum=1)
        except ValueError as exc:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_query", "detail": str(exc)},
            )
        try:
            events = self._dashboard.events(project_id, item_id, limit=limit)
        except (KeyError, ValueError, StorageError):
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        return self._respond(
            start_response,
            HTTPStatus.OK,
            {"project_id": project_id, "item_id": item_id, "events": events},
        )

    def _daily_report_get(
        self,
        start_response: Any,
        method: str,
    ) -> Iterable[bytes]:
        if method != "GET":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "GET")],
            )
        if self._daily_reports is None:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "daily_report_not_configured"},
            )
        try:
            snapshot = self._daily_reports.snapshot()
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "daily_report_unavailable"},
            )
        return self._respond(start_response, HTTPStatus.OK, snapshot)

    def _daily_report_action(
        self,
        environ: dict[str, Any],
        start_response: Any,
        method: str,
        action: str,
    ) -> Iterable[bytes]:
        if method != "POST":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "POST")],
            )
        if self._daily_reports is None:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "daily_report_not_configured"},
            )
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return self._respond(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "content_type_must_be_application_json"},
            )
        try:
            payload = (
                {}
                if int(environ.get("CONTENT_LENGTH") or 0) == 0
                else self._read_json(environ)
            )
            if payload is None:
                payload = {}
            if not isinstance(payload, dict):
                raise ValueError("request body must be a JSON object")
            report_id = payload.get("report_id")
            if report_id is not None and (
                not isinstance(report_id, str) or not report_id.strip()
            ):
                raise ValueError("report_id must be a non-empty string")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_daily_report_request", "detail": str(exc)},
            )
        try:
            result = (
                self._daily_reports.mark_viewed(report_id)
                if action == "view"
                else self._daily_reports.mark_dismissed(report_id)
            )
        except DailyReportNotFoundError as exc:
            return self._respond(
                start_response,
                HTTPStatus.NOT_FOUND,
                {"error": "daily_report_not_found", "detail": str(exc)},
            )
        except (DailyReportError, StorageError) as exc:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "daily_report_unavailable", "detail": str(exc)},
            )
        except Exception:
            return self._respond(
                start_response,
                HTTPStatus.SERVICE_UNAVAILABLE,
                {"error": "daily_report_unavailable"},
            )
        return self._respond(
            start_response,
            HTTPStatus.OK,
            result,
        )

    def _bridge_source_claim(
        self,
        environ: dict[str, Any],
        start_response: Any,
        method: str,
    ) -> Iterable[bytes]:
        if self._bridge is None:
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        if method != "POST":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "POST")],
            )
        content_type = environ.get("CONTENT_TYPE", "").split(";", 1)[0].strip()
        if content_type != "application/json":
            return self._respond(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "content_type_must_be_application_json"},
            )
        try:
            payload = self._read_json(environ)
            platform = _payload_text(payload, "platform")
            session_id = _payload_text(payload, "session_id")
            question = payload.get("user_question")
            turn_id = payload.get("turn_id")
            if question is not None and not isinstance(question, str):
                raise ValueError("invalid user question")
            if turn_id is not None and (not isinstance(turn_id, str) or not turn_id.strip()):
                raise ValueError("invalid turn id")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_bridge_request", "detail": str(exc)},
            )
        source = self._bridge.claim_resume_source(platform, session_id, user_question=question, turn_id=turn_id)
        return self._respond(
            start_response,
            HTTPStatus.OK,
            {"source_suggestion_id": source},
        )

    @staticmethod
    def _read_json(environ: dict[str, Any]) -> Any:
        content_length = int(environ.get("CONTENT_LENGTH") or 0)
        raw_body = environ["wsgi.input"].read(content_length)
        return json.loads(raw_body.decode("utf-8"))

    def _serve_asset(
        self,
        start_response: Any,
        asset: str,
        content_type: str,
    ) -> list[bytes]:
        path = self._web_root / asset
        if not path.is_file():
            return self._respond(start_response, HTTPStatus.NOT_FOUND, {"error": "not_found"})
        encoded = path.read_bytes()
        start_response(
            f"{HTTPStatus.OK.value} {HTTPStatus.OK.phrase}",
            [
                ("Content-Type", content_type),
                ("Content-Length", str(len(encoded))),
                ("Cache-Control", "no-cache"),
            ],
        )
        return [encoded]

    @staticmethod
    def _respond(
        start_response: Any,
        status: HTTPStatus,
        body: dict[str, Any],
        *,
        headers: list[tuple[str, str]] | None = None,
    ) -> list[bytes]:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode(
            "utf-8"
        )
        response_headers = [
            ("Content-Type", "application/json; charset=utf-8"),
            ("Content-Length", str(len(encoded))),
        ]
        if headers:
            response_headers.extend(headers)
        start_response(f"{status.value} {status.phrase}", response_headers)
        return [encoded]


def create_app(
    handler: InboundEventHandler | None = None,
    *,
    bridge: BridgeHub | None = None,
    store: MarkdownStore | None = None,
    journal: RuntimeJournal | None = None,
    daily_reports: DailyReportService | None = None,
) -> EventApplication:
    return EventApplication(
        handler,
        bridge=bridge,
        store=store,
        journal=journal,
        daily_reports=daily_reports,
    )


application = create_app()


def _query_text(query: dict[str, list[str]], field: str) -> str:
    values = query.get(field, [])
    if len(values) != 1 or not values[0].strip():
        raise ValueError(f"{field} must be provided exactly once")
    return values[0].strip()


def _query_int(
    query: dict[str, list[str]],
    field: str,
    *,
    default: int,
    minimum: int,
) -> int:
    values = query.get(field, [])
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"{field} must be provided at most once")
    try:
        value = int(values[0])
    except ValueError as exc:
        raise ValueError(f"{field} must be an integer") from exc
    if value < minimum:
        raise ValueError(f"{field} must be >= {minimum}")
    return value


def _query_float(
    query: dict[str, list[str]],
    field: str,
    *,
    default: float,
    minimum: float,
    maximum: float,
) -> float:
    values = query.get(field, [])
    if not values:
        return default
    if len(values) != 1:
        raise ValueError(f"{field} must be provided at most once")
    try:
        value = float(values[0])
    except ValueError as exc:
        raise ValueError(f"{field} must be a number") from exc
    if not minimum <= value <= maximum:
        raise ValueError(f"{field} must be between {minimum} and {maximum}")
    return value


def _payload_text(payload: Any, field: str) -> str:
    if not isinstance(payload, dict):
        raise ValueError("payload must be a JSON object")
    value = payload.get(field)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()
