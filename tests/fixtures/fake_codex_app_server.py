"""Deterministic Codex app-server fixture for Connector tests."""

from __future__ import annotations

import json
import sys
from typing import Any


THREAD_ID = "thread-codex-1"
turn_number = 0


def send(message: dict[str, Any]) -> None:
    # Codex app-server omits jsonrpc on the wire.
    print(json.dumps(message, ensure_ascii=False, separators=(",", ":")), flush=True)


for line in sys.stdin:
    request = json.loads(line)
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params", {})

    if method == "initialize":
        send(
            {
                "id": request_id,
                "result": {
                    "serverInfo": {"name": "fake-codex", "version": "1.0"},
                    "capabilities": {"threads": True},
                },
            }
        )
        continue
    if method == "initialized":
        continue
    if method == "thread/start":
        send(
            {
                "id": request_id,
                "result": {
                    "thread": {
                        "id": THREAD_ID,
                        "sessionId": THREAD_ID,
                    }
                },
            }
        )
        continue
    if method == "thread/resume":
        if params.get("threadId") != THREAD_ID:
            send(
                {
                    "id": request_id,
                    "error": {"code": -32001, "message": "unknown thread"},
                }
            )
        else:
            send(
                {
                    "id": request_id,
                    "result": {"thread": {"id": THREAD_ID, "sessionId": THREAD_ID}},
                }
            )
        continue
    if method == "turn/start":
        turn_number += 1
        turn_id = f"turn-codex-{turn_number}"
        message_id = f"message-{turn_number}"
        answer = (
            "Codex 第一段，Codex 第二段。"
            if turn_number == 1
            else f"Codex 第{turn_number}轮第一段，Codex 第{turn_number}轮第二段。"
        )
        send(
            {
                "id": request_id,
                "result": {
                    "turn": {
                        "id": turn_id,
                        "status": "inProgress",
                        "items": [],
                    }
                },
            }
        )
        send({"method": "turn/started", "params": {"turn": {"id": turn_id}}})
        send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "itemId": message_id,
                    "delta": "Codex 第一段，",
                },
            }
        )
        send(
            {
                "method": "item/agentMessage/delta",
                "params": {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "itemId": message_id,
                    "delta": "Codex 第二段。",
                },
            }
        )
        send(
            {
                "method": "item/completed",
                "params": {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "item": {
                        "type": "agentMessage",
                        "id": message_id,
                        "text": answer,
                    },
                },
            }
        )
        send(
            {
                "method": "turn/completed",
                "params": {
                    "threadId": THREAD_ID,
                    "turnId": turn_id,
                    "turn": {"id": turn_id, "status": "completed"},
                },
            }
        )
        continue
    # Unknown requests get a normal JSON-RPC error response.
    if request_id is not None:
        send(
            {
                "id": request_id,
                "error": {"code": -32601, "message": f"unknown method: {method}"},
            }
        )
