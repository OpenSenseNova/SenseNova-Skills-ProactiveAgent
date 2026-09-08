"""Deterministic ACP v1 Agent used by connector integration tests."""

from __future__ import annotations

import json
import sys
from typing import Any


SESSION_ID = "fake-session-1"
MODE = sys.argv[1] if len(sys.argv) > 1 else "basic"
restored_session_id: str | None = None


def send(message: dict[str, Any]) -> None:
    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)


def result(request_id: int, payload: Any) -> None:
    send({"jsonrpc": "2.0", "id": request_id, "result": payload})


def error(request_id: int, code: int, message: str) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "error": {"code": code, "message": message},
        }
    )


def update(payload: dict[str, Any]) -> None:
    send(
        {
            "jsonrpc": "2.0",
            "method": "session/update",
            "params": {"sessionId": SESSION_ID, "update": payload},
        }
    )


for line in sys.stdin:
    request = json.loads(line)
    request_id = request.get("id")
    method = request.get("method")

    if method == "initialize":
        capabilities: dict[str, Any] = {
            "loadSession": MODE == "load",
            "promptCapabilities": {
                "image": False,
                "audio": False,
                "embeddedContext": False,
            },
        }
        if MODE in {"resume", "resume-error"}:
            capabilities["sessionCapabilities"] = {"resume": {}}
        result(
            request_id,
            {
                "protocolVersion": 1,
                "agentCapabilities": capabilities,
                "agentInfo": {
                    "name": "fake-acp-agent",
                    "version": "1.0.0",
                },
            },
        )
        continue

    if method == "session/new":
        if MODE != "basic":
            error(request_id, -32001, "session/new is forbidden in restore mode")
            continue
        result(request_id, {"sessionId": SESSION_ID})
        continue

    if method == "session/resume":
        if MODE not in {"resume", "resume-error"}:
            error(request_id, -32601, "session/resume is not supported")
            continue
        params = request.get("params", {})
        if params.get("sessionId") != SESSION_ID:
            error(request_id, -32002, "unknown session")
            continue
        if MODE == "resume-error":
            error(request_id, -32003, "session restore failed")
            continue
        restored_session_id = SESSION_ID
        result(request_id, {})
        continue

    if method == "session/load":
        if MODE != "load":
            error(request_id, -32601, "session/load is not supported")
            continue
        params = request.get("params", {})
        if params.get("sessionId") != SESSION_ID:
            error(request_id, -32002, "unknown session")
            continue
        update(
            {
                "sessionUpdate": "user_message_chunk",
                "messageId": "replayed-user-message",
                "content": {"type": "text", "text": "历史问题"},
            }
        )
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "replayed-agent-message",
                "content": {"type": "text", "text": "历史回答"},
            }
        )
        restored_session_id = SESSION_ID
        result(request_id, None)
        continue

    if method == "session/prompt":
        params = request.get("params", {})
        if params.get("sessionId") != SESSION_ID:
            error(request_id, -32002, "prompt targeted a different session")
            continue
        if MODE in {"resume", "load", "resume-error"} and (
            restored_session_id != SESSION_ID
        ):
            error(request_id, -32004, "session was not restored")
            continue
        prompt = params.get("prompt", [])
        question = prompt[0].get("text", "") if prompt else ""
        update(
            {
                "sessionUpdate": "plan",
                "entries": [
                    {"content": "生成测试回答", "priority": "high", "status": "pending"}
                ],
            }
        )
        first_chunk = "已在原 Session " if MODE in {"resume", "load"} else "第一段回答，"
        second_chunk = "继续执行。" if MODE in {"resume", "load"} else "第二段回答。"
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "fake-answer-1",
                "content": {"type": "text", "text": first_chunk},
            }
        )
        update(
            {
                "sessionUpdate": "usage_update",
                "used": 10,
                "size": 1000,
            }
        )
        update(
            {
                "sessionUpdate": "agent_message_chunk",
                "messageId": "fake-answer-1",
                "content": {"type": "text", "text": second_chunk},
            }
        )
        stop_reason = "cancelled" if question == "__cancel__" else "end_turn"
        result(request_id, {"stopReason": stop_reason})
        continue

    error(request_id, -32601, f"unknown method: {method}")
