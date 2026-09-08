"""Map Codex app-server notifications to the V1 event contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from proactive_memory_service.contracts import TurnCompleted, TurnStarted

from .session import CodexTurnBuffer


class CodexMappingError(ValueError):
    """Raised when a Codex message cannot be mapped safely."""


class CodexTurnIncomplete(CodexMappingError):
    """Raised when Codex ends a turn without a successful final answer."""


Clock = Callable[[], datetime]


class CodexV1Mapper:
    def __init__(self, platform: str = "codex", *, clock: Clock | None = None) -> None:
        if not isinstance(platform, str) or not platform.strip():
            raise ValueError("platform must be a non-empty string")
        self.platform = platform
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def begin_turn(
        self,
        *,
        thread_id: str,
        turn_id: str,
        user_question: str,
        source_suggestion_id: str | None = None,
    ) -> tuple[CodexTurnBuffer, TurnStarted]:
        for field_name, value in (
            ("thread_id", thread_id),
            ("turn_id", turn_id),
            ("user_question", user_question),
        ):
            if not isinstance(value, str) or not value.strip():
                raise CodexMappingError(f"{field_name} must be a non-empty string")
        if source_suggestion_id is not None and (
            not isinstance(source_suggestion_id, str)
            or not source_suggestion_id.strip()
        ):
            raise CodexMappingError(
                "source_suggestion_id must be non-empty when provided"
            )
        started_at = self.timestamp()
        return (
            CodexTurnBuffer(
                thread_id=thread_id,
                turn_id=turn_id,
                user_question=user_question,
                started_at=started_at,
                source_suggestion_id=source_suggestion_id,
            ),
            TurnStarted(
                platform=self.platform,
                session_id=thread_id,
                turn_id=turn_id,
                started_at=started_at,
            ),
        )

    def apply_notification(
        self,
        buffer: CodexTurnBuffer,
        method: str,
        params: Mapping[str, Any],
    ) -> bool:
        thread_id = params.get("threadId")
        if thread_id is not None and thread_id != buffer.thread_id:
            return False
        turn_id = params.get("turnId")
        if turn_id is not None and turn_id != buffer.turn_id:
            return False
        if method == "item/agentMessage/delta":
            item_id = params.get("itemId")
            delta = params.get("delta")
            if not isinstance(item_id, str) or not item_id.strip():
                raise CodexMappingError("agent message delta must contain itemId")
            if not isinstance(delta, str):
                raise CodexMappingError("agent message delta must contain delta text")
            buffer.append_delta(item_id, delta)
            return True
        if method == "item/completed":
            item = params.get("item")
            if not isinstance(item, Mapping) or item.get("type") != "agentMessage":
                return False
            item_id = item.get("id")
            text = item.get("text")
            if not isinstance(item_id, str) or not item_id.strip():
                raise CodexMappingError("agentMessage item must contain id")
            if not isinstance(text, str):
                raise CodexMappingError("agentMessage item must contain text")
            buffer.set_message(item_id, text)
            return True
        return False

    def complete_turn(
        self,
        buffer: CodexTurnBuffer,
        *,
        status: str,
    ) -> TurnCompleted:
        if status != "completed":
            raise CodexTurnIncomplete(f"Codex turn did not complete successfully: {status}")
        if not buffer.final_answer.strip():
            raise CodexTurnIncomplete("Codex turn ended without a final text answer")
        return TurnCompleted(
            platform=self.platform,
            session_id=buffer.thread_id,
            turn_id=buffer.turn_id,
            user_question=buffer.user_question,
            final_answer=buffer.final_answer,
            completed_at=self.timestamp(),
            source_suggestion_id=buffer.source_suggestion_id,
        )

    def timestamp(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise CodexMappingError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise CodexMappingError("clock must return a timezone-aware datetime")
        return value

