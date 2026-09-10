"""V1 event structures defined by the Proactive Agent integration contract."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any, ClassVar, Mapping, TypeAlias


class ContractValidationError(ValueError):
    """Raised when an event payload does not satisfy the V1 contract."""


ConnectorId: TypeAlias = str


class EventType(StrEnum):
    TURN_STARTED = "turn.started"
    TURN_COMPLETED = "turn.completed"
    SUGGESTION_READY = "suggestion.ready"
    SUGGESTION_RESPONDED = "suggestion.responded"
    SESSION_RESUME_REQUESTED = "session.resume.requested"
    SESSION_RESUME_FAILED = "session.resume.failed"


class ToolExecutionStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class SuggestionChoice(StrEnum):
    APPROVE = "approve"
    IGNORE = "ignore"


def _object(payload: Mapping[str, Any] | object) -> Mapping[str, Any]:
    if not isinstance(payload, Mapping):
        raise ContractValidationError("payload must be a JSON object")
    return payload


def _check_keys(
    payload: Mapping[str, Any],
    *,
    required: set[str],
    optional: set[str] | None = None,
) -> None:
    optional = optional or set()
    missing = required - payload.keys()
    if missing:
        raise ContractValidationError(
            f"missing required fields: {', '.join(sorted(missing))}"
        )
    unknown = payload.keys() - required - optional
    if unknown:
        raise ContractValidationError(f"unknown fields: {', '.join(sorted(unknown))}")


def _text(payload: Mapping[str, Any], field: str) -> str:
    value = payload[field]
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string")
    return value


def _optional_text(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ContractValidationError(f"{field} must be a non-empty string when set")
    return value


def _enum(payload: Mapping[str, Any], field: str, enum_type: type[StrEnum]) -> StrEnum:
    value = _text(payload, field)
    try:
        return enum_type(value)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in enum_type)
        raise ContractValidationError(f"{field} must be one of: {allowed}") from exc


def _timestamp(payload: Mapping[str, Any], field: str) -> datetime:
    value = _text(payload, field)
    normalized = f"{value[:-1]}+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError as exc:
        raise ContractValidationError(f"{field} must be an RFC 3339 timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ContractValidationError(f"{field} must include a timezone")
    return parsed


def _format_timestamp(value: datetime) -> str:
    rendered = value.isoformat()
    return f"{rendered[:-6]}Z" if rendered.endswith("+00:00") else rendered


@dataclass(frozen=True, slots=True)
class ToolExecutionEvidence:
    tool_name: str
    action_summary: str
    status: ToolExecutionStatus
    artifact_refs: tuple[str, ...] = ()

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> ToolExecutionEvidence:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={"tool_name", "action_summary", "status"},
            optional={"artifact_refs"},
        )
        raw_refs = payload.get("artifact_refs", [])
        if not isinstance(raw_refs, list):
            raise ContractValidationError("artifact_refs must be an array")
        refs: list[str] = []
        for index, value in enumerate(raw_refs):
            if not isinstance(value, str) or not value.strip():
                raise ContractValidationError(
                    f"artifact_refs[{index}] must be a non-empty string"
                )
            refs.append(value)
        return cls(
            tool_name=_text(payload, "tool_name"),
            action_summary=_text(payload, "action_summary"),
            status=_enum(payload, "status", ToolExecutionStatus),
            artifact_refs=tuple(refs),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "tool_name": self.tool_name,
            "action_summary": self.action_summary,
            "status": self.status.value,
        }
        if self.artifact_refs:
            payload["artifact_refs"] = list(self.artifact_refs)
        return payload


@dataclass(frozen=True, slots=True)
class TurnStarted:
    """A lightweight freshness signal emitted when a user starts a new turn."""

    event_type: ClassVar[EventType] = EventType.TURN_STARTED

    platform: ConnectorId
    session_id: str
    turn_id: str
    started_at: datetime

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> TurnStarted:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={"platform", "session_id", "turn_id", "started_at"},
        )
        return cls(
            platform=_text(payload, "platform"),
            session_id=_text(payload, "session_id"),
            turn_id=_text(payload, "turn_id"),
            started_at=_timestamp(payload, "started_at"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "platform": self.platform,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "started_at": _format_timestamp(self.started_at),
        }


@dataclass(frozen=True, slots=True)
class TurnCompleted:
    event_type: ClassVar[EventType] = EventType.TURN_COMPLETED

    platform: ConnectorId
    session_id: str
    turn_id: str
    user_question: str
    final_answer: str
    completed_at: datetime
    tool_execution_evidence: tuple[ToolExecutionEvidence, ...] = ()
    source_suggestion_id: str | None = None

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> TurnCompleted:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={
                "platform",
                "session_id",
                "turn_id",
                "user_question",
                "final_answer",
                "completed_at",
            },
            optional={"tool_execution_evidence", "source_suggestion_id"},
        )
        raw_evidence = payload.get("tool_execution_evidence", [])
        if not isinstance(raw_evidence, list):
            raise ContractValidationError("tool_execution_evidence must be an array")
        evidence = tuple(
            ToolExecutionEvidence.from_payload(item) for item in raw_evidence
        )
        return cls(
            platform=_text(payload, "platform"),
            session_id=_text(payload, "session_id"),
            turn_id=_text(payload, "turn_id"),
            user_question=_text(payload, "user_question"),
            final_answer=_text(payload, "final_answer"),
            completed_at=_timestamp(payload, "completed_at"),
            tool_execution_evidence=evidence,
            source_suggestion_id=_optional_text(payload, "source_suggestion_id"),
        )

    def to_payload(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "platform": self.platform,
            "session_id": self.session_id,
            "turn_id": self.turn_id,
            "user_question": self.user_question,
            "final_answer": self.final_answer,
            "completed_at": _format_timestamp(self.completed_at),
        }
        if self.tool_execution_evidence:
            payload["tool_execution_evidence"] = [
                item.to_payload() for item in self.tool_execution_evidence
            ]
        if self.source_suggestion_id is not None:
            payload["source_suggestion_id"] = self.source_suggestion_id
        return payload


@dataclass(frozen=True, slots=True)
class SuggestionReady:
    event_type: ClassVar[EventType] = EventType.SUGGESTION_READY

    suggestion_id: str
    platform: ConnectorId
    created_at: datetime
    target_session_id: str
    title: str
    why_now: str
    evidence: str
    suggested_action: str

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> SuggestionReady:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={
                "suggestion_id",
                "platform",
                "created_at",
                "target_session_id",
                "title",
                "why_now",
                "evidence",
                "suggested_action",
            },
        )
        return cls(
            suggestion_id=_text(payload, "suggestion_id"),
            platform=_text(payload, "platform"),
            created_at=_timestamp(payload, "created_at"),
            target_session_id=_text(payload, "target_session_id"),
            title=_text(payload, "title"),
            why_now=_text(payload, "why_now"),
            evidence=_text(payload, "evidence"),
            suggested_action=_text(payload, "suggested_action"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "platform": self.platform,
            "created_at": _format_timestamp(self.created_at),
            "target_session_id": self.target_session_id,
            "title": self.title,
            "why_now": self.why_now,
            "evidence": self.evidence,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class SuggestionResponded:
    event_type: ClassVar[EventType] = EventType.SUGGESTION_RESPONDED

    suggestion_id: str
    choice: SuggestionChoice
    responded_at: datetime

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> SuggestionResponded:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={"suggestion_id", "choice", "responded_at"},
        )
        return cls(
            suggestion_id=_text(payload, "suggestion_id"),
            choice=_enum(payload, "choice", SuggestionChoice),
            responded_at=_timestamp(payload, "responded_at"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "choice": self.choice.value,
            "responded_at": _format_timestamp(self.responded_at),
        }


@dataclass(frozen=True, slots=True)
class SessionResumeRequested:
    event_type: ClassVar[EventType] = EventType.SESSION_RESUME_REQUESTED

    suggestion_id: str
    platform: ConnectorId
    target_session_id: str
    suggested_action: str

    @classmethod
    def from_payload(
        cls, raw_payload: Mapping[str, Any] | object
    ) -> SessionResumeRequested:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={
                "suggestion_id",
                "platform",
                "target_session_id",
                "suggested_action",
            },
        )
        return cls(
            suggestion_id=_text(payload, "suggestion_id"),
            platform=_text(payload, "platform"),
            target_session_id=_text(payload, "target_session_id"),
            suggested_action=_text(payload, "suggested_action"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "platform": self.platform,
            "target_session_id": self.target_session_id,
            "suggested_action": self.suggested_action,
        }


@dataclass(frozen=True, slots=True)
class SessionResumeFailed:
    event_type: ClassVar[EventType] = EventType.SESSION_RESUME_FAILED

    suggestion_id: str
    reason: str
    failed_at: datetime

    @classmethod
    def from_payload(cls, raw_payload: Mapping[str, Any] | object) -> SessionResumeFailed:
        payload = _object(raw_payload)
        _check_keys(
            payload,
            required={"suggestion_id", "reason", "failed_at"},
        )
        return cls(
            suggestion_id=_text(payload, "suggestion_id"),
            reason=_text(payload, "reason"),
            failed_at=_timestamp(payload, "failed_at"),
        )

    def to_payload(self) -> dict[str, Any]:
        return {
            "suggestion_id": self.suggestion_id,
            "reason": self.reason,
            "failed_at": _format_timestamp(self.failed_at),
        }


Event: TypeAlias = (
    TurnStarted
    | TurnCompleted
    | SuggestionReady
    | SuggestionResponded
    | SessionResumeRequested
    | SessionResumeFailed
)
InboundEvent: TypeAlias = (
    TurnStarted | TurnCompleted | SuggestionResponded | SessionResumeFailed
)
OutboundEvent: TypeAlias = SuggestionReady | SessionResumeRequested

INBOUND_EVENT_TYPES = frozenset(
    {
        EventType.TURN_STARTED,
        EventType.TURN_COMPLETED,
        EventType.SUGGESTION_RESPONDED,
        EventType.SESSION_RESUME_FAILED,
    }
)
OUTBOUND_EVENT_TYPES = frozenset(
    {EventType.SUGGESTION_READY, EventType.SESSION_RESUME_REQUESTED}
)

_EVENT_MODELS = {
    EventType.TURN_STARTED: TurnStarted,
    EventType.TURN_COMPLETED: TurnCompleted,
    EventType.SUGGESTION_READY: SuggestionReady,
    EventType.SUGGESTION_RESPONDED: SuggestionResponded,
    EventType.SESSION_RESUME_REQUESTED: SessionResumeRequested,
    EventType.SESSION_RESUME_FAILED: SessionResumeFailed,
}


def parse_event(event_type: EventType | str, payload: Mapping[str, Any] | object) -> Event:
    """Validate and parse a V1 event payload."""

    try:
        normalized_type = EventType(event_type)
    except ValueError as exc:
        raise ContractValidationError(f"unknown event type: {event_type}") from exc
    return _EVENT_MODELS[normalized_type].from_payload(payload)


def event_to_envelope(event: Event) -> dict[str, Any]:
    """Serialize an event with an explicit wire-level event type."""

    return {"event_type": event.event_type.value, "payload": event.to_payload()}
