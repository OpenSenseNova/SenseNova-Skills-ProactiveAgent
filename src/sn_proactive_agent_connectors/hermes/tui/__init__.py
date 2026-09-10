"""Hermes observer bridge for the Proactive Agent TUI Connector."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import threading
import urllib.request
from datetime import datetime, timezone
from typing import Any, Mapping

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_SOURCE_BY_TURN: dict[tuple[str, str], str] = {}
_EVIDENCE_BY_TURN: dict[tuple[str, str], list[dict[str, Any]]] = {}


def _env(name: str, default: str | None = None) -> str | None:
    # This plugin is copied into Hermes; it cannot import the pipx runtime.
    legacy = name.replace("SN_PROACTIVE_AGENT_", "PROACTIVE_MEMORY_", 1)
    return os.environ.get(name, os.environ.get(legacy, default))


def register(ctx: Any) -> None:
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("post_tool_call", on_post_tool_call)


def on_post_tool_call(**kwargs: Any) -> None:
    if not _is_tui(kwargs):
        return
    session_id = _text(kwargs.get("session_id"))
    turn_id = _turn_id(kwargs)
    tool_name = _text(kwargs.get("tool_name"))
    if not session_id or not turn_id or not tool_name:
        return
    raw_status = _text(kwargs.get("status")).lower()
    status = "failed" if raw_status in {"blocked", "cancelled", "error", "failed", "timeout"} else "succeeded"
    evidence = {
        "tool_name": tool_name,
        "action_summary": f"Hermes 调用了 {tool_name}",
        "status": status,
    }
    with _LOCK:
        key = (session_id, turn_id)
        bucket = _EVIDENCE_BY_TURN.setdefault(key, [])
        bucket.append(evidence)
        del bucket[:-32]


def on_pre_llm_call(**kwargs: Any) -> None:
    if not _is_tui(kwargs):
        return
    session_id = _text(kwargs.get("session_id"))
    turn_id = _turn_id(kwargs)
    if not session_id or not turn_id:
        return

    # Claim before emitting turn.started so the resulting turn.completed can
    # carry the source ID even if the model starts immediately.
    source_id = _claim_source(session_id, _text(kwargs.get("user_message")), turn_id)
    if source_id:
        with _LOCK:
            _SOURCE_BY_TURN[(session_id, turn_id)] = source_id

    _post(
        "turn.started",
        {
            "platform": "hermes-tui",
            "session_id": session_id,
            "turn_id": turn_id,
            "started_at": _now(),
        },
    )


def on_post_llm_call(**kwargs: Any) -> None:
    if not _is_tui(kwargs):
        return
    session_id = _text(kwargs.get("session_id"))
    turn_id = _turn_id(kwargs)
    question = _text(kwargs.get("user_message"))
    answer = _text(kwargs.get("assistant_response"))
    if not session_id or not turn_id or not question or not answer:
        return

    with _LOCK:
        source_id = _SOURCE_BY_TURN.pop((session_id, turn_id), None)
        evidence = _EVIDENCE_BY_TURN.pop((session_id, turn_id), [])

    payload: dict[str, Any] = {
        "platform": "hermes-tui",
        "session_id": session_id,
        "turn_id": turn_id,
        "user_question": question,
        "final_answer": answer,
        "completed_at": _now(),
    }
    if source_id:
        payload["source_suggestion_id"] = source_id

    if evidence:
        payload["tool_execution_evidence"] = evidence
    _post("turn.completed", payload)


def _is_tui(kwargs: Mapping[str, Any]) -> bool:
    platform = _text(kwargs.get("platform")).lower()
    requested = _env("SN_PROACTIVE_AGENT_HERMES_PLATFORMS", "tui")
    allowed = {item.strip().lower() for item in requested.split(",") if item.strip()}
    return platform in allowed


def _turn_id(kwargs: Mapping[str, Any]) -> str:
    value = _text(kwargs.get("turn_id"))
    if value:
        return value
    session_id = _text(kwargs.get("session_id"))
    question = _text(kwargs.get("user_message"))
    seed = f"{session_id}\0{question}"
    return f"turn-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"


def _claim_source(session_id: str, user_question: str, turn_id: str) -> str | None:
    request = urllib.request.Request(
        f"{_service_url()}/v1/bridge/source/claim",
        data=json.dumps(
            {"platform": "hermes-tui", "session_id": session_id, "user_question": user_question, "turn_id": turn_id},
            ensure_ascii=False,
        ).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            raw = json.loads(response.read().decode("utf-8"))
        value = raw.get("source_suggestion_id") if isinstance(raw, dict) else None
        return value if isinstance(value, str) and value.strip() else None
    except Exception as exc:
        _debug(f"source claim failed: {exc}")
        return None


def _post(event_name: str, payload: dict[str, Any]) -> None:
    request = urllib.request.Request(
        f"{_service_url()}/v1/events/{event_name}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=_timeout()) as response:
            response.read()
    except Exception as exc:
        _debug(f"{event_name} failed: {exc}")


def _service_url() -> str:
    return _env(
        "SN_PROACTIVE_AGENT_SERVICE_URL", "http://127.0.0.1:8080"
    ).rstrip("/")


def _timeout() -> float:
    try:
        return max(
            0.2,
            min(3.0, float(_env("SN_PROACTIVE_AGENT_HTTP_TIMEOUT", "1.5"))),
        )
    except ValueError:
        return 1.5


def _now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _debug(message: str) -> None:
    if _env("SN_PROACTIVE_AGENT_DEBUG") == "1":
        logger.warning("sn-proactive-agent: %s", message)
