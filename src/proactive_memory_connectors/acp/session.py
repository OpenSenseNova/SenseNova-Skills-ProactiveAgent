"""In-memory state used while one ACP prompt turn is being assembled."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping


@dataclass(frozen=True, slots=True)
class AcpInitialization:
    protocol_version: int
    agent_capabilities: Mapping[str, Any]
    agent_info: Mapping[str, Any] | None = None


@dataclass(slots=True)
class AcpTurnBuffer:
    session_id: str
    turn_id: str
    user_question: str
    started_at: datetime
    source_suggestion_id: str | None = None
    _message_ids: list[str | None] = field(default_factory=list, repr=False)
    _message_chunks: list[list[str]] = field(default_factory=list, repr=False)

    def append_agent_text(self, text: str, message_id: str | None) -> None:
        if not text:
            return
        if not self._message_ids or self._message_ids[-1] != message_id:
            self._message_ids.append(message_id)
            self._message_chunks.append([])
        self._message_chunks[-1].append(text)

    @property
    def final_answer(self) -> str:
        messages = ["".join(chunks) for chunks in self._message_chunks]
        return "\n".join(message for message in messages if message)


@dataclass(slots=True)
class AcpSession:
    session_id: str
    cwd: Path
    mcp_servers: tuple[Mapping[str, Any], ...] = ()
    restored_with: str | None = None
    active_turn: AcpTurnBuffer | None = None
