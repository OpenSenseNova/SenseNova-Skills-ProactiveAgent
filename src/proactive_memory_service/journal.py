"""Compact append-only JSONL journal for operational lifecycle records."""

from __future__ import annotations

import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import SuggestionReady, TurnCompleted, TurnStarted
from .storage import event_id_for_turn


class JournalError(RuntimeError):
    """Raised when the runtime journal cannot be safely parsed or updated."""


@dataclass(frozen=True, slots=True)
class RuntimeRecord:
    record_id: str
    kind: str
    recorded_at: str
    payload: Mapping[str, Any]


_RECORD_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,191}\Z")


class RuntimeJournal:
    """Keep raw Turns, decisions, suggestions, and resume results in JSONL."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.path = self.data_root / "runtime.jsonl"
        legacy_path = self.data_root / "runtime.md"
        self._lock = threading.RLock()
        if legacy_path.exists() and not self.path.exists():
            raise JournalError(
                f"legacy runtime journal found at {legacy_path}; "
                "run scripts/migrate_runtime_journal.py first"
            )
        if legacy_path.exists() and self.path.exists():
            raise JournalError(
                f"both runtime.jsonl and legacy runtime.md exist in {self.data_root}"
            )
        if not self.path.exists():
            _atomic_write(self.path, "")

    def append(
        self,
        kind: str,
        record_id: str,
        payload: Mapping[str, Any],
        *,
        recorded_at: datetime | None = None,
    ) -> bool:
        """Append one idempotent record and return whether it was new."""

        if not isinstance(kind, str) or not kind.strip():
            raise ValueError("journal kind must be a non-empty string")
        if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
            raise ValueError("journal record id contains unsupported characters")
        timestamp = (recorded_at or datetime.now(timezone.utc)).isoformat()
        body: dict[str, Any] = {
            "record_id": record_id,
            "kind": kind,
            "recorded_at": timestamp,
            "payload": dict(payload),
        }
        line = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            if any(
                record.record_id == record_id
                for record in self._read_records_locked()
            ):
                return False
            _append_line(self.path, line)
            return True

    def records(self, kind: str | None = None) -> tuple[RuntimeRecord, ...]:
        with self._lock:
            records = self._read_records_locked()
        if kind is None:
            return records
        return tuple(record for record in records if record.kind == kind)

    def _read_records_locked(self) -> tuple[RuntimeRecord, ...]:
        """Read and validate the JSONL file while the journal lock is held."""

        if not self.path.exists():
            return ()
        records: list[RuntimeRecord] = []
        seen_ids: set[str] = set()
        text = self.path.read_text(encoding="utf-8")
        for line_number, line in enumerate(text.splitlines(), start=1):
            if not line.strip():
                continue
            try:
                raw = json.loads(line)
            except json.JSONDecodeError as exc:
                raise JournalError(
                    f"invalid JSON in runtime.jsonl line {line_number}"
                ) from exc
            if not isinstance(raw, dict):
                raise JournalError(
                    f"runtime.jsonl line {line_number} must be a JSON object"
                )
            record_id = raw.get("record_id")
            if not isinstance(record_id, str) or not _RECORD_ID.fullmatch(record_id):
                raise JournalError(
                    f"invalid runtime record id on line {line_number}"
                )
            if record_id in seen_ids:
                raise JournalError(f"duplicate runtime record id: {record_id}")
            kind = raw.get("kind")
            if not isinstance(kind, str) or not kind.strip():
                raise JournalError(
                    f"runtime record kind must be non-empty on line {line_number}"
                )
            recorded_at = raw.get("recorded_at")
            if not isinstance(recorded_at, str) or not recorded_at.strip():
                raise JournalError(
                    f"runtime record timestamp must be non-empty on line {line_number}"
                )
            payload = raw.get("payload")
            if not isinstance(payload, dict):
                raise JournalError(
                    f"runtime record payload must be an object on line {line_number}"
                )
            seen_ids.add(record_id)
            records.append(
                RuntimeRecord(
                    record_id=record_id,
                    kind=kind,
                    recorded_at=recorded_at,
                    payload=payload,
                )
            )
        return tuple(records)

    def append_turn_started(self, event: TurnStarted) -> bool:
        record_id = _source_record_id(
            "started", event.platform, event.session_id, event.turn_id
        )
        return self.append(
            "turn.started",
            record_id,
            event.to_payload(),
            recorded_at=event.started_at,
        )

    def append_turn_completed(self, event: TurnCompleted) -> bool:
        return self.append(
            "turn.completed",
            f"turn-{event_id_for_turn(event).removeprefix('event-')}",
            event.to_payload(),
            recorded_at=event.completed_at,
        )

    def latest_started_turn(self, platform: str, session_id: str) -> str | None:
        for record in reversed(self.records("turn.started")):
            payload = record.payload
            if (
                payload.get("platform") == platform
                and payload.get("session_id") == session_id
            ):
                turn_id = payload.get("turn_id")
                return turn_id if isinstance(turn_id, str) else None
        return None

    def has_decision(self, event_id: str) -> bool:
        return any(
            record.payload.get("event_id") == event_id
            for record in self.records("judge.decision")
        )

    def get_suggestion(self, suggestion_id: str) -> SuggestionReady | None:
        for record in reversed(self.records("suggestion.ready")):
            if record.payload.get("suggestion_id") == suggestion_id:
                return SuggestionReady.from_payload(record.payload)
        return None

    def has_suggestion_response(self, suggestion_id: str) -> bool:
        return any(
            record.payload.get("suggestion_id") == suggestion_id
            for record in self.records("suggestion.responded")
        )

    def latest_assignment(
        self,
        platform: str,
        session_id: str,
    ) -> tuple[str, str] | None:
        matching_event_ids: set[str] = set()
        for record in self.records("turn.completed"):
            try:
                turn = TurnCompleted.from_payload(record.payload)
            except Exception:
                continue
            if turn.platform == platform and turn.session_id == session_id:
                matching_event_ids.add(event_id_for_turn(turn))
        for record in reversed(self.records("organizer.result")):
            payload = record.payload
            if payload.get("outcome") != "assigned":
                continue
            if payload.get("event_id") not in matching_event_ids:
                continue
            project_id = payload.get("project_id")
            item_id = payload.get("item_id")
            if isinstance(project_id, str) and isinstance(item_id, str):
                return project_id, item_id
        return None


def _source_record_id(prefix: str, platform: str, session_id: str, turn_id: str) -> str:
    import hashlib

    raw = "\0".join((platform, session_id, turn_id)).encode("utf-8")
    return f"{prefix}-{hashlib.sha256(raw).hexdigest()[:24]}"


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise


def _append_line(path: Path, line: str) -> None:
    """Append one complete JSONL record and flush it durably."""

    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8", newline="\n") as stream:
        stream.write(line)
        stream.write("\n")
        stream.flush()
        os.fsync(stream.fileno())
