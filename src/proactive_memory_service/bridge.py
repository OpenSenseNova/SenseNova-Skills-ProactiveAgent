"""Session-scoped delivery bridge for interactive Harness UIs.

The Proactive Memory Core emits semantic outbound events.  Interactive
Connectors use :class:`BridgeHub` to deliver those events to a live UI without
opening another terminal or depending on an operating-system window API.
"""

from __future__ import annotations

import threading
import time
import secrets
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from .contracts import TurnCompleted, TurnStarted


BridgeKey = tuple[str, str]


@dataclass(frozen=True, slots=True)
class BridgeRecord:
    """One ordered event visible to a connected UI session."""

    sequence: int
    event_type: str
    platform: str
    session_id: str
    payload: Mapping[str, Any]

    def to_payload(self) -> dict[str, Any]:
        return {
            "id": f"bridge-{self.sequence}",
            "sequence": self.sequence,
            "event_type": self.event_type,
            "platform": self.platform,
            "session_id": self.session_id,
            "payload": dict(self.payload),
        }


class BridgeHub:
    """Small thread-safe, session-scoped event buffer.

    The first TUI implementation uses bounded long-polling rather than a
    platform-specific IPC primitive.  A client keeps the returned cursor and
    asks for events after that cursor, so reconnects do not duplicate events.
    The buffer is intentionally bounded: the durable RuntimeJournal remains
    the source of truth, while this hub only holds live UI delivery state.
    """

    def __init__(
        self, *, max_events_per_session: int = 128,
        clock: Callable[[], float] = time.monotonic, lease_seconds: float = 15.0,
    ) -> None:
        if max_events_per_session < 1:
            raise ValueError("max_events_per_session must be positive")
        self.max_events_per_session = max_events_per_session
        self._condition = threading.Condition(threading.RLock())
        self._next_sequence = 0
        self._events: dict[BridgeKey, deque[BridgeRecord]] = defaultdict(deque)
        self._pending_sources: dict[BridgeKey, deque[str]] = defaultdict(deque)
        self._clock = clock
        self._lease_seconds = lease_seconds
        self._clients: dict[BridgeKey, dict[str, Any]] = {}
        self._resumes: dict[tuple[str, str, str], dict[str, Any]] = {}
        self._turns: dict[BridgeKey, dict[str, Any]] = {}

    def publish(
        self,
        event_type: str,
        platform: str,
        session_id: str,
        payload: Mapping[str, Any],
    ) -> BridgeRecord:
        """Publish one event to exactly one platform/session stream."""

        if not event_type.strip():
            raise ValueError("event_type must be a non-empty string")
        if not platform.strip():
            raise ValueError("platform must be a non-empty string")
        if not session_id.strip():
            raise ValueError("session_id must be a non-empty string")
        key = (platform, session_id)
        with self._condition:
            self._next_sequence += 1
            record = BridgeRecord(
                sequence=self._next_sequence,
                event_type=event_type,
                platform=platform,
                session_id=session_id,
                payload=dict(payload),
            )
            events = self._events[key]
            events.append(record)
            if event_type == "session.resume.requested":
                suggestion_id = str(payload.get("suggestion_id", ""))
                self._resumes.setdefault((platform, session_id, suggestion_id), {
                    "action": payload.get("suggested_action"), "claimed": False,
                    "created": self._clock(), "completed": False,
                })
            while len(events) > self.max_events_per_session:
                events.popleft()
            self._condition.notify_all()
            return record

    def queue_resume_source(
        self,
        platform: str,
        session_id: str,
        suggestion_id: str,
    ) -> None:
        """Make a suggestion ID available to the next successful turn.

        The TUI submits the authorized action through its live Session.  Its
        normal ``post_llm_call`` hook can then claim this ID and include it in
        ``turn.completed`` without putting a hidden marker in the transcript.
        """

        if not suggestion_id.strip():
            raise ValueError("suggestion_id must be a non-empty string")
        key = (platform, session_id)
        with self._condition:
            pending = self._pending_sources[key]
            # V1 permits one active suggestion per Session.  Avoid accumulating
            # duplicate source IDs if a client retries the resume notification.
            if suggestion_id not in pending:
                pending.append(suggestion_id)
            self._condition.notify_all()

    def claim_resume_source(
        self, platform: str, session_id: str, *, user_question: str | None = None,
        turn_id: str | None = None,
    ) -> str | None:
        """Claim the source ID for the next turn, if an authorized resume exists."""

        key = (platform, session_id)
        with self._condition:
            pending = self._pending_sources.get(key)
            if not pending:
                return None
            if user_question is not None:
                record = self._resumes.get((platform, session_id, pending[0]))
                if record and (record["completed"] or self._clock() - record["created"] > 120):
                    pending.popleft()
                    return None
                if not record or record["action"] != user_question:
                    return None
                if (platform, session_id) in self._clients and not record["claimed"]:
                    return None
                if turn_id:
                    record["source_turn_id"] = turn_id
            source = pending.popleft()
            if not pending:
                self._pending_sources.pop(key, None)
            return source

    def cancel_resume(self, suggestion_id: str) -> None:
        with self._condition:
            for key in list(self._resumes):
                if key[2] == suggestion_id:
                    self._resumes.pop(key, None)
                    pending = self._pending_sources.get(key[:2])
                    if pending and suggestion_id in pending:
                        pending.remove(suggestion_id)

    def heartbeat(
        self, platform: str, session_id: str, *, client_id: str,
        instance_id: str, probe: str = "", ready: bool = False,
    ) -> dict[str, Any]:
        """Lease one live window. Probe echoes prove a live round trip, not execution."""
        key = (platform, session_id)
        with self._condition:
            now = self._clock()
            old = self._clients.get(key)
            if old and now - old["seen"] < self._lease_seconds:
                if old["client_id"] != client_id or old["instance_id"] != instance_id:
                    raise ValueError("another live window owns this Session")
            if not old or old["client_id"] != client_id or now - old["seen"] >= self._lease_seconds:
                old = {
                    "client_id": client_id, "instance_id": instance_id,
                    "probe": secrets.token_hex(16), "roundtrip": False,
                }
                self._turns.pop(key, None)
            old["roundtrip"] = old["roundtrip"] or bool(probe and probe == old["probe"])
            old.update(seen=now, ready=ready)
            self._clients[key] = old
            self._prune_live_state(now)
            return {"probe": old["probe"], "roundtrip": old["roundtrip"]}

    def claim_resume(
        self, platform: str, session_id: str, *, client_id: str,
        instance_id: str, suggestion_id: str,
    ) -> dict[str, Any]:
        """At-most-once dispatch of an action already published by Core after approval."""
        with self._condition:
            client = self._clients.get((platform, session_id))
            if (
                not client or self._clock() - client["seen"] >= self._lease_seconds
                or client["client_id"] != client_id or client["instance_id"] != instance_id
                or not client["roundtrip"] or not client["ready"]
            ):
                raise ValueError("Session bridge is not ready")
            record = self._resumes.get((platform, session_id, suggestion_id))
            if not record or record["claimed"] or self._clock() - record["created"] > 120:
                return {"claimed": False}
            record.update(claimed=True, client_id=client_id, instance_id=instance_id)
            return {"claimed": True, "suggested_action": record["action"]}

    def observe_turn(self, event: TurnStarted | TurnCompleted) -> None:
        """Keep only receipt IDs; never put QA text in diagnostics."""
        key = (event.platform, event.session_id)
        with self._condition:
            client = self._clients.get(key)
            if not client or self._clock() - client["seen"] >= self._lease_seconds:
                return
            state = self._turns.setdefault(key, {})
            if isinstance(event, TurnStarted):
                state["started"] = event.turn_id
                return
            if state.get("started") != event.turn_id:
                return
            state["completed"] = event.turn_id
            if not event.source_suggestion_id:
                return
            record = self._resumes.get((*key, event.source_suggestion_id))
            if (
                record and record["claimed"] and record["action"] == event.user_question
                and record.get("source_turn_id") == event.turn_id
                and record["client_id"] == client["client_id"]
                and record["instance_id"] == client["instance_id"]
            ):
                record["completed"] = True
                state["resume_completed"] = event.source_suggestion_id

    def diagnostics(self, platform: str, session_id: str, instance_id: str) -> dict[str, Any]:
        with self._condition:
            key = (platform, session_id)
            client = self._clients.get(key)
            online = bool(
                client and client["instance_id"] == instance_id
                and self._clock() - client["seen"] < self._lease_seconds
            )
            state = self._turns.get(key, {}) if online else {}
            return {
                "protocol": 1, "platform": platform, "session_id": session_id,
                "instance_id": instance_id, "online": online,
                "roundtrip": bool(online and client["roundtrip"]),
                "ready": bool(online and client["ready"]),
                "turn_completed": state.get("completed"),
                "resume_completed": state.get("resume_completed"),
            }

    def _prune_live_state(self, now: float) -> None:
        for key in list(self._clients):
            if now - self._clients[key]["seen"] > 300:
                self._clients.pop(key, None)
                self._turns.pop(key, None)
        for key in list(self._resumes):
            if now - self._resumes[key]["created"] > 3600:
                self._resumes.pop(key, None)

    def poll(
        self,
        platform: str,
        session_id: str,
        *,
        after: int = 0,
        timeout_seconds: float = 0.0,
    ) -> tuple[int, tuple[BridgeRecord, ...]]:
        """Return events after *after*, waiting at most *timeout_seconds*."""

        if after < 0:
            raise ValueError("after must be non-negative")
        timeout_seconds = max(0.0, timeout_seconds)
        deadline = time.monotonic() + timeout_seconds
        key = (platform, session_id)
        with self._condition:
            while True:
                events = self._events.get(key, ())
                visible = tuple(record for record in events if record.sequence > after)
                if visible:
                    return visible[-1].sequence, visible
                if timeout_seconds <= 0:
                    return after, ()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return after, ()
                self._condition.wait(timeout=remaining)
