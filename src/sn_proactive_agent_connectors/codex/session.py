"""In-memory Codex thread and turn state."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path


@dataclass(frozen=True, slots=True)
class CodexInitialization:
    server_info: dict[str, object] | None = None
    capabilities: dict[str, object] = field(default_factory=dict)


@dataclass(slots=True)
class CodexTurnBuffer:
    thread_id: str
    turn_id: str
    user_question: str
    started_at: datetime
    source_suggestion_id: str | None = None
    _agent_messages: dict[str, list[str]] = field(default_factory=dict, repr=False)
    _message_order: list[str] = field(default_factory=list, repr=False)

    def append_delta(self, item_id: str, delta: str) -> None:
        if not delta:
            return
        if item_id not in self._agent_messages:
            self._agent_messages[item_id] = []
            self._message_order.append(item_id)
        self._agent_messages[item_id].append(delta)

    def set_message(self, item_id: str, text: str) -> None:
        if item_id not in self._agent_messages:
            self._message_order.append(item_id)
        self._agent_messages[item_id] = [text]

    @property
    def final_answer(self) -> str:
        return "\n".join(
            "".join(self._agent_messages[item_id])
            for item_id in self._message_order
            if "".join(self._agent_messages[item_id]).strip()
        )


@dataclass(slots=True)
class CodexThread:
    thread_id: str
    session_id: str
    cwd: Path
    active_turn: CodexTurnBuffer | None = None
    restored: bool = False

