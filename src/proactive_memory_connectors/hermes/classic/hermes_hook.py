#!/usr/bin/env python3
"""Map Hermes shell-hook payloads to Proactive Memory V1 HTTP events."""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.error
import urllib.request
from datetime import datetime, timezone
from typing import Any


def main() -> None:
    if os.environ.get("PROACTIVE_MEMORY_INTERNAL_REASONER") == "1":
        print("{}")
        return
    try:
        payload = json.load(sys.stdin)
        event_name, event_payload = _map_payload(payload)
        if event_name is not None:
            _post(event_name, event_payload)
    except Exception as exc:
        print(f"proactive-memory hook: {type(exc).__name__}: {exc}", file=sys.stderr)
    print("{}")


def _map_payload(payload: Any) -> tuple[str | None, dict[str, Any]]:
    if not isinstance(payload, dict):
        raise ValueError("hook payload must be a JSON object")
    hook = payload.get("hook_event_name")
    session_id = _text(payload.get("session_id"), "session_id")
    extra = payload.get("extra")
    if not isinstance(extra, dict):
        extra = {}
    # The native TUI plugin reports TUI turns directly. Keep this legacy shell
    # hook for CLI only so one TUI turn is never ingested twice.
    surface = extra.get("platform")
    if isinstance(surface, str) and surface.strip().lower() in {"tui", "desktop"}:
        return None, {}
    turn_id = extra.get("turn_id")
    if not isinstance(turn_id, str) or not turn_id.strip():
        seed = f"{session_id}\0{extra.get('user_message', '')}\0{hook}"
        turn_id = f"turn-{hashlib.sha256(seed.encode('utf-8')).hexdigest()[:20]}"
    now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

    if hook == "pre_llm_call":
        return "turn.started", {
            "platform": "hermes-cli",
            "session_id": session_id,
            "turn_id": turn_id,
            "started_at": now,
        }
    if hook == "post_llm_call":
        question = extra.get("user_message")
        answer = extra.get("assistant_response")
        if not isinstance(question, str) or not question.strip():
            return None, {}
        if not isinstance(answer, str) or not answer.strip():
            return None, {}
        completed: dict[str, Any] = {
            "platform": "hermes-cli",
            "session_id": session_id,
            "turn_id": turn_id,
            "user_question": question,
            "final_answer": answer,
            "completed_at": now,
        }
        source_suggestion_id = os.environ.get(
            "PROACTIVE_MEMORY_SOURCE_SUGGESTION_ID"
        )
        if source_suggestion_id:
            completed["source_suggestion_id"] = source_suggestion_id
        return "turn.completed", completed
    return None, {}


def _post(event_name: str, payload: dict[str, Any]) -> None:
    base_url = os.environ.get(
        "PROACTIVE_MEMORY_SERVICE_URL", "http://127.0.0.1:8080"
    ).rstrip("/")
    request = urllib.request.Request(
        f"{base_url}/v1/events/{event_name}",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=1.5) as response:
            response.read()
    except (OSError, urllib.error.URLError) as exc:
        raise RuntimeError(f"service unavailable: {exc}") from exc


def _text(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value


if __name__ == "__main__":
    main()
