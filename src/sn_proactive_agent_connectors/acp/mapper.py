"""Map ACP v1 prompt-turn messages to the V1 Proactive Agent contract."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from typing import Any

from sn_proactive_agent.contracts import TurnCompleted, TurnStarted

from .session import AcpTurnBuffer


class AcpMappingError(ValueError):
    """Raised when a recognized ACP message cannot be mapped safely."""


class AcpTurnIncomplete(AcpMappingError):
    """Raised when a prompt stops without a successful final answer."""


Clock = Callable[[], datetime]


class AcpV1Mapper:
    def __init__(
        self,
        platform: str,
        *,
        clock: Clock | None = None,
    ) -> None:
        if not isinstance(platform, str) or not platform.strip():
            raise ValueError("platform must be a non-empty string")
        self.platform = platform
        self.clock = clock or (lambda: datetime.now(timezone.utc))

    def begin_turn(
        self,
        *,
        session_id: str,
        turn_id: str,
        user_question: str,
        source_suggestion_id: str | None = None,
    ) -> tuple[AcpTurnBuffer, TurnStarted]:
        for field_name, value in (
            ("session_id", session_id),
            ("turn_id", turn_id),
            ("user_question", user_question),
        ):
            if not isinstance(value, str) or not value.strip():
                raise AcpMappingError(f"{field_name} must be a non-empty string")
        if source_suggestion_id is not None and (
            not isinstance(source_suggestion_id, str)
            or not source_suggestion_id.strip()
        ):
            raise AcpMappingError(
                "source_suggestion_id must be non-empty when provided"
            )

        started_at = self.timestamp()
        buffer = AcpTurnBuffer(
            session_id=session_id,
            turn_id=turn_id,
            user_question=user_question,
            started_at=started_at,
            source_suggestion_id=source_suggestion_id,
        )
        return buffer, TurnStarted(
            platform=self.platform,
            session_id=session_id,
            turn_id=turn_id,
            started_at=started_at,
        )

    def apply_session_update(
        self,
        buffer: AcpTurnBuffer,
        update: Mapping[str, Any],
    ) -> bool:
        """Consume answer text; safely ignore unrelated ACP update variants."""

        if update.get("sessionUpdate") != "agent_message_chunk":
            return False
        content = update.get("content")
        if not isinstance(content, Mapping):
            raise AcpMappingError("agent_message_chunk.content must be an object")
        if content.get("type") != "text":
            return False
        text = content.get("text")
        if not isinstance(text, str):
            raise AcpMappingError("text content must contain a string")
        message_id = update.get("messageId")
        if message_id is not None and not isinstance(message_id, str):
            raise AcpMappingError("messageId must be a string when provided")
        buffer.append_agent_text(text, message_id)
        return True

    def complete_turn(
        self,
        buffer: AcpTurnBuffer,
        *,
        stop_reason: str,
    ) -> TurnCompleted:
        if stop_reason != "end_turn":
            raise AcpTurnIncomplete(
                f"ACP prompt did not complete successfully: {stop_reason}"
            )
        if not buffer.final_answer.strip():
            raise AcpTurnIncomplete("ACP prompt ended without a final text answer")
        return TurnCompleted(
            platform=self.platform,
            session_id=buffer.session_id,
            turn_id=buffer.turn_id,
            user_question=buffer.user_question,
            final_answer=buffer.final_answer,
            completed_at=self.timestamp(),
            source_suggestion_id=buffer.source_suggestion_id,
        )

    def timestamp(self) -> datetime:
        value = self.clock()
        if not isinstance(value, datetime):
            raise AcpMappingError("clock must return datetime")
        if value.tzinfo is None or value.utcoffset() is None:
            raise AcpMappingError("clock must return a timezone-aware datetime")
        return value
