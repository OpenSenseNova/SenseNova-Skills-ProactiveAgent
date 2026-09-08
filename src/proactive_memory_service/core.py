"""Harness-neutral Proactive Memory core orchestration."""

from __future__ import annotations

import hashlib
import queue
import threading
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Callable, Protocol

from .connector import ConnectorRouter
from .contracts import (
    InboundEvent,
    SessionResumeFailed,
    SessionResumeRequested,
    SuggestionChoice,
    SuggestionReady,
    SuggestionResponded,
    TurnCompleted,
    TurnStarted,
)
from .journal import RuntimeJournal
from .storage import (
    ItemState,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
    event_id_for_turn,
)

if TYPE_CHECKING:
    from .semantic import JudgeResult, OrganizationResult


@dataclass(frozen=True, slots=True)
class OrganizerContext:
    """Read-only-intended workspace injected by the shared Core runtime."""

    data_root: Path
    projects: tuple[ProjectMetadata, ...]
    recent_project_id: str | None = None
    recent_item_id: str | None = None


class TurnOrganizer(Protocol):
    """Run inside Core, inspect shared files, and return a storage update."""

    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> OrganizedTurn:
        """Choose one Project / Item without directly mutating storage."""


class CoreOrganizer(Protocol):
    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> "OrganizationResult":
        """Return a complete V1 organization plan or a justified skip."""


class CoreJudge(Protocol):
    def judge(
        self,
        turn: TurnCompleted,
        project: ProjectMetadata,
        item: ItemState,
        event_summary: str,
    ) -> "JudgeResult":
        """Return a suggestion or a documented silent decision."""


class TurnStorageHandler:
    """Persist completed Turns after an Organizer has made semantic decisions."""

    def __init__(self, store: MarkdownStore, organizer: TurnOrganizer) -> None:
        self.store = store
        self.organizer = organizer

    def handle(self, event: InboundEvent) -> None:
        if not isinstance(event, TurnCompleted):
            return
        context = OrganizerContext(
            data_root=self.store.data_root,
            projects=self.store.list_projects(),
        )
        organized = self.organizer.organize(event, context)
        self.store.apply_turn(event, organized)


class ProactiveMemoryCore:
    """Run the complete state-update, freshness, and suggestion lifecycle."""

    def __init__(
        self,
        store: MarkdownStore,
        journal: RuntimeJournal,
        organizer: CoreOrganizer,
        judge: CoreJudge,
        connectors: ConnectorRouter,
        *,
        asynchronous: bool = True,
        log: Callable[[str], None] = print,
    ) -> None:
        self.store = store
        self.journal = journal
        self.organizer = organizer
        self.judge = judge
        self.connectors = connectors
        self.asynchronous = asynchronous
        self.log = log
        self._pending: set[str] = set()
        self._pending_lock = threading.Lock()
        self._queue: queue.Queue[TurnCompleted | None] = queue.Queue()
        self._worker: threading.Thread | None = None
        if asynchronous:
            self._worker = threading.Thread(
                target=self._work,
                name="proactive-memory-worker",
                daemon=True,
            )
            self._worker.start()

    def handle(self, event: InboundEvent) -> None:
        if isinstance(event, TurnStarted):
            self.journal.append_turn_started(event)
            self.log(
                f"[activity] {event.platform}/{event.session_id}/{event.turn_id} started"
            )
            return
        if isinstance(event, TurnCompleted):
            self._accept_completed_turn(event)
            return
        if isinstance(event, SuggestionResponded):
            self._handle_suggestion_response(event)
            return
        if isinstance(event, SessionResumeFailed):
            self.journal.append(
                "session.resume.failed",
                f"resume-failed-{_safe_suffix(event.suggestion_id)}",
                event.to_payload(),
                recorded_at=event.failed_at,
            )
            self.log(f"[resume failed] {event.suggestion_id}: {event.reason}")

    def wait_until_idle(self) -> None:
        if self.asynchronous:
            self._queue.join()

    def close(self) -> None:
        if self._worker is None:
            return
        self._queue.put(None)
        self._worker.join(timeout=5)
        self._worker = None

    def _accept_completed_turn(self, turn: TurnCompleted) -> None:
        self.journal.append_turn_completed(turn)
        event_id = event_id_for_turn(turn)
        if self.journal.has_decision(event_id):
            self.log(f"[dedupe] {event_id} already has a terminal decision")
            return
        with self._pending_lock:
            if event_id in self._pending:
                self.log(f"[dedupe] {event_id} is already being processed")
                return
            self._pending.add(event_id)
        if self.asynchronous:
            self._queue.put(turn)
        else:
            self._process_safely(turn)

    def _work(self) -> None:
        while True:
            turn = self._queue.get()
            try:
                if turn is None:
                    return
                self._process_safely(turn)
            finally:
                self._queue.task_done()

    def _process_safely(self, turn: TurnCompleted) -> None:
        event_id = event_id_for_turn(turn)
        try:
            self._process_turn(turn)
        except Exception as exc:  # Keep the hook-facing service alive and retryable.
            self.journal.append(
                "processing.failed",
                f"failure-{event_id.removeprefix('event-')}-{uuid.uuid4().hex[:8]}",
                {
                    "event_id": event_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            self.log(f"[processing failed] {event_id}: {type(exc).__name__}: {exc}")
        finally:
            with self._pending_lock:
                self._pending.discard(event_id)

    def _process_turn(self, turn: TurnCompleted) -> None:
        # Import here to keep protocols acyclic and lightweight for contract-only use.
        from .semantic import OrganizationPlan, OrganizationSkipped

        event_id = event_id_for_turn(turn)
        recent = self.journal.latest_assignment(turn.platform, turn.session_id)
        context = OrganizerContext(
            data_root=self.store.data_root,
            projects=self.store.list_projects(),
            recent_project_id=recent[0] if recent is not None else None,
            recent_item_id=recent[1] if recent is not None else None,
        )
        organization = self.organizer.organize(turn, context)
        if isinstance(organization, OrganizationSkipped):
            self.journal.append(
                "organizer.result",
                f"organized-{event_id.removeprefix('event-')}",
                {
                    "event_id": event_id,
                    "outcome": "untracked",
                    "reason": organization.reason,
                    "platform": turn.platform,
                    "session_id": turn.session_id,
                    "turn_id": turn.turn_id,
                },
            )
            self._record_decision(
                event_id,
                outcome="silent",
                reason=f"未进入项目状态：{organization.reason}",
            )
            self.log(f"[silent] {event_id}: {organization.reason}")
            return
        if not isinstance(organization, OrganizationPlan):
            raise TypeError("Organizer returned an unsupported result")

        self.store.ensure_project(organization.project)
        self.store.ensure_item(organization.project.id, organization.initial_item)
        applied = self.store.apply_turn(turn, organization.event)
        self.journal.append(
            "organizer.result",
            f"organized-{event_id.removeprefix('event-')}",
            {
                "event_id": event_id,
                "outcome": "assigned",
                "project_id": organization.project.id,
                "item_id": organization.initial_item.id,
                "reason": organization.reason,
                "event_appended": applied.appended,
                "platform": turn.platform,
                "session_id": turn.session_id,
                "turn_id": turn.turn_id,
            },
        )
        self.log(
            f"[organized] {event_id} -> "
            f"{organization.project.id}/{organization.initial_item.id}"
        )

        if turn.source_suggestion_id is not None:
            self.journal.append(
                "suggestion.executed",
                f"executed-{_safe_suffix(turn.source_suggestion_id)}",
                {
                    "suggestion_id": turn.source_suggestion_id,
                    "event_id": event_id,
                    "project_id": organization.project.id,
                    "item_id": organization.initial_item.id,
                },
            )
            self._record_decision(
                event_id,
                outcome="source_suggestion_completed",
                reason="本轮由已授权建议触发；只更新状态，不连续生成下一条建议。",
                project_id=organization.project.id,
                item_id=organization.initial_item.id,
            )
            self.log(f"[completed authorized suggestion] {turn.source_suggestion_id}")
            return

        latest_turn = self.journal.latest_started_turn(turn.platform, turn.session_id)
        if latest_turn is not None and latest_turn != turn.turn_id:
            self._record_decision(
                event_id,
                outcome="discarded",
                reason=(
                    f"Session 已开始更新的 Turn {latest_turn}；保留状态更新，"
                    "不再生成旧建议。"
                ),
                project_id=organization.project.id,
                item_id=organization.initial_item.id,
            )
            self.log(f"[discarded] {event_id}: newer turn {latest_turn}")
            return

        baseline_turn = latest_turn
        judged = self.judge.judge(
            turn,
            organization.project,
            applied.item,
            organization.event.summary,
        )
        newest_turn = self.journal.latest_started_turn(turn.platform, turn.session_id)
        if newest_turn != baseline_turn:
            self._record_decision(
                event_id,
                outcome="discarded",
                reason=(
                    f"Judge 运行期间 Session 开始了更新的 Turn {newest_turn}；"
                    "保留状态更新，丢弃判断结果。"
                ),
                project_id=organization.project.id,
                item_id=organization.initial_item.id,
            )
            self.log(f"[discarded] {event_id}: activity changed during Judge")
            return

        if not judged.should_suggest:
            self._record_decision(
                event_id,
                outcome="silent",
                reason=judged.reason,
                project_id=organization.project.id,
                item_id=organization.initial_item.id,
            )
            self.log(f"[silent] {event_id}: {judged.reason}")
            return

        suggestion_id = _suggestion_id(event_id)
        suggestion = SuggestionReady(
            suggestion_id=suggestion_id,
            platform=turn.platform,
            created_at=datetime.now(timezone.utc),
            target_session_id=turn.session_id,
            title=judged.title or "建议继续推进",
            why_now=judged.reason,
            evidence=judged.evidence or organization.event.summary,
            suggested_action=judged.suggested_action or applied.item.next_step,
        )
        self._record_decision(
            event_id,
            outcome="suggest",
            reason=judged.reason,
            project_id=organization.project.id,
            item_id=organization.initial_item.id,
            suggestion_id=suggestion_id,
        )
        self.journal.append(
            "suggestion.ready",
            suggestion_id,
            suggestion.to_payload(),
            recorded_at=suggestion.created_at,
        )
        try:
            self.connectors.dispatch(suggestion)
        except Exception as exc:
            self.journal.append(
                "suggestion.delivery.failed",
                f"delivery-{_safe_suffix(suggestion_id)}",
                {
                    "suggestion_id": suggestion_id,
                    "error_type": type(exc).__name__,
                    "reason": str(exc),
                },
            )
            self.log(f"[suggestion delivery failed] {suggestion_id}: {exc}")

    def _record_decision(
        self,
        event_id: str,
        *,
        outcome: str,
        reason: str,
        project_id: str | None = None,
        item_id: str | None = None,
        suggestion_id: str | None = None,
    ) -> None:
        payload: dict[str, Any] = {
            "event_id": event_id,
            "outcome": outcome,
            "reason": reason,
        }
        if project_id is not None:
            payload["project_id"] = project_id
        if item_id is not None:
            payload["item_id"] = item_id
        if suggestion_id is not None:
            payload["suggestion_id"] = suggestion_id
        self.journal.append(
            "judge.decision",
            f"decision-{event_id.removeprefix('event-')}",
            payload,
        )

    def _handle_suggestion_response(self, event: SuggestionResponded) -> None:
        suggestion = self.journal.get_suggestion(event.suggestion_id)
        if suggestion is None:
            raise ValueError(f"unknown suggestion: {event.suggestion_id}")
        if self.journal.has_suggestion_response(event.suggestion_id):
            self.log(f"[dedupe] suggestion already responded: {event.suggestion_id}")
            return
        self.journal.append(
            "suggestion.responded",
            f"response-{_safe_suffix(event.suggestion_id)}",
            event.to_payload(),
            recorded_at=event.responded_at,
        )
        if event.choice == SuggestionChoice.IGNORE:
            self.log(f"[ignored] {event.suggestion_id}")
            return
        request = SessionResumeRequested(
            suggestion_id=suggestion.suggestion_id,
            platform=suggestion.platform,
            target_session_id=suggestion.target_session_id,
            suggested_action=suggestion.suggested_action,
        )
        self.journal.append(
            "session.resume.requested",
            f"resume-{_safe_suffix(event.suggestion_id)}",
            request.to_payload(),
        )
        self.connectors.dispatch(request)


def _suggestion_id(event_id: str) -> str:
    digest = hashlib.sha256(event_id.encode("utf-8")).hexdigest()[:20]
    return f"suggestion-{digest}"


def _safe_suffix(value: str) -> str:
    normalized = "".join(
        character if character.isascii() and character.isalnum() else "-"
        for character in value.lower()
    ).strip("-")
    return (normalized or hashlib.sha256(value.encode("utf-8")).hexdigest()[:16])[:96]
