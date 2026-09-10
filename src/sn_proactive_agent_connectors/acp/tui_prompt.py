"""Adapter-only HTTP route that lets Hermes TUI submit through ACP."""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from http import HTTPStatus
from typing import Any, Protocol

from sn_proactive_agent.contracts import TurnCompleted


class WsgiApplication(Protocol):
    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Any,
    ) -> Iterable[bytes]: ...


class PromptClient(Protocol):
    def prompt(self, session_id: str, user_question: str) -> TurnCompleted: ...


class AcpTuiPromptApplication:
    """Route visible Hermes TUI submissions into exact ACP Sessions.

    This is a Connector endpoint, not a new Core event.  The ACP Client still
    emits the normal ``turn.started`` and ``turn.completed`` events, so Core,
    storage, Organizer, and Judge stay Harness-neutral.  Strict single-Session
    routing is the default.  A caller may explicitly provide ``session_factory``
    and a bounded ``max_sessions`` for visible ``/new`` workflows.
    """

    route = "/v1/connectors/acp/prompt"
    max_body_bytes = 1024 * 1024

    def __init__(
        self,
        downstream: WsgiApplication,
        client: PromptClient,
        *,
        session_id: str,
        session_factory: Callable[[], str] | None = None,
        max_sessions: int = 1,
    ) -> None:
        if not isinstance(session_id, str) or not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        if (
            not isinstance(max_sessions, int)
            or isinstance(max_sessions, bool)
            or max_sessions < 1
        ):
            raise ValueError("max_sessions must be a positive integer")
        if session_factory is not None and max_sessions < 2:
            raise ValueError("session_factory requires max_sessions >= 2")
        self.downstream = downstream
        self.client = client
        self.session_id = session_id.strip()
        self.session_factory = session_factory
        self.max_sessions = max_sessions
        self._session_aliases = {self.session_id: self.session_id}
        self._actual_session_ids = {self.session_id}
        self._prompt_lock = threading.Lock()

    def __call__(
        self,
        environ: dict[str, Any],
        start_response: Any,
    ) -> Iterable[bytes]:
        if environ.get("PATH_INFO", "") != self.route:
            return self.downstream(environ, start_response)

        method = str(environ.get("REQUEST_METHOD", "GET")).upper()
        if method != "POST":
            return self._respond(
                start_response,
                HTTPStatus.METHOD_NOT_ALLOWED,
                {"error": "method_not_allowed"},
                headers=[("Allow", "POST")],
            )
        content_type = str(environ.get("CONTENT_TYPE", "")).split(";", 1)[0].strip()
        if content_type != "application/json":
            return self._respond(
                start_response,
                HTTPStatus.UNSUPPORTED_MEDIA_TYPE,
                {"error": "content_type_must_be_application_json"},
            )
        try:
            content_length = int(environ.get("CONTENT_LENGTH") or 0)
            if not 0 < content_length <= self.max_body_bytes:
                raise ValueError("invalid content length")
            raw = environ["wsgi.input"].read(content_length)
            payload = json.loads(raw.decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("payload must be an object")
            session_id = self._required_text(payload, "session_id")
            user_question = self._required_text(payload, "user_question")
        except (KeyError, TypeError, ValueError, UnicodeDecodeError, json.JSONDecodeError):
            return self._respond(
                start_response,
                HTTPStatus.BAD_REQUEST,
                {"error": "invalid_acp_prompt"},
            )
        if session_id not in self._session_aliases and self.session_factory is None:
            return self._respond(
                start_response,
                HTTPStatus.CONFLICT,
                {"error": "session_mismatch"},
            )
        if not self._prompt_lock.acquire(blocking=False):
            return self._respond(
                start_response,
                HTTPStatus.CONFLICT,
                {"error": "acp_session_busy"},
            )
        try:
            target_session_id = self._session_aliases.get(session_id)
            if target_session_id is None:
                if len(self._actual_session_ids) >= self.max_sessions:
                    return self._respond(
                        start_response,
                        HTTPStatus.CONFLICT,
                        {"error": "session_limit_reached"},
                    )
                try:
                    target_session_id = self.session_factory() if self.session_factory else ""
                except Exception as exc:
                    return self._respond(
                        start_response,
                        HTTPStatus.BAD_GATEWAY,
                        {
                            "error": "acp_session_create_failed",
                            "detail": f"{type(exc).__name__}: {exc}",
                        },
                    )
                if not isinstance(target_session_id, str) or not target_session_id.strip():
                    return self._respond(
                        start_response,
                        HTTPStatus.BAD_GATEWAY,
                        {"error": "acp_session_create_failed"},
                    )
                target_session_id = target_session_id.strip()
                self._session_aliases[session_id] = target_session_id
                self._session_aliases[target_session_id] = target_session_id
                self._actual_session_ids.add(target_session_id)
            turn = self.client.prompt(target_session_id, user_question)
        except Exception as exc:
            return self._respond(
                start_response,
                HTTPStatus.BAD_GATEWAY,
                {
                    "error": "acp_prompt_failed",
                    "detail": f"{type(exc).__name__}: {exc}",
                },
            )
        finally:
            self._prompt_lock.release()

        payload = turn.to_payload()
        return self._respond(
            start_response,
            HTTPStatus.OK,
            {
                "accepted": True,
                "platform": turn.platform,
                "session_id": turn.session_id,
                "requested_session_id": session_id,
                "session_rebound": turn.session_id != session_id,
                "turn_id": turn.turn_id,
                "final_answer": turn.final_answer,
                "completed_at": payload["completed_at"],
            },
        )

    @staticmethod
    def _required_text(payload: dict[str, Any], field: str) -> str:
        value = payload.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError(f"{field} must be a non-empty string")
        return value.strip()

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
