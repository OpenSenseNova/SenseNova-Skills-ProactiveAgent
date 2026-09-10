"""Markdown-backed Project, Item, and QA Event storage."""

from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
import threading
from dataclasses import dataclass, replace
from pathlib import Path
from types import MappingProxyType
from typing import Any, Mapping

from .contracts import TurnCompleted


class StorageError(RuntimeError):
    """Base class for storage failures."""


class StorageNotFoundError(StorageError):
    """Raised when a requested Project or Item does not exist."""


class StorageConflictError(StorageError):
    """Raised when an existing record conflicts with a requested write."""


class StorageFormatError(StorageError):
    """Raised when a managed Markdown file does not match the V1 format."""


_ID_PATTERN = re.compile(r"[a-z0-9][a-z0-9._-]{0,127}\Z")
_PROJECT_STATUSES = frozenset({"active", "paused", "completed"})
_ITEM_STATUSES = frozenset({"planned", "in_progress", "blocked", "completed"})
_ITEM_UPDATE_FIELDS = (
    "status",
    "goal",
    "completion_criteria",
    "current_progress",
    "next_step",
    "blocker",
)
_ITEM_SECTION_NAMES = {
    "目标": "goal",
    "完成标准": "completion_criteria",
    "当前进展": "current_progress",
    "下一步": "next_step",
    "阻塞": "blocker",
}


def _identifier(value: str, field: str) -> str:
    if not isinstance(value, str) or not _ID_PATTERN.fullmatch(value):
        raise ValueError(
            f"{field} must use lowercase letters, digits, dots, underscores, or hyphens"
        )
    return value


def _text(value: str, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{field} must be a non-empty string")
    return value.strip()


def _single_line(value: str, field: str) -> str:
    normalized = _text(value, field)
    if "\n" in normalized or "\r" in normalized:
        raise ValueError(f"{field} must be a single line")
    return normalized


@dataclass(frozen=True, slots=True)
class ProjectMetadata:
    id: str
    name: str
    summary: str
    status: str = "active"

    def __post_init__(self) -> None:
        _identifier(self.id, "project id")
        _single_line(self.name, "project name")
        _single_line(self.summary, "project summary")
        if self.status not in _PROJECT_STATUSES:
            allowed = ", ".join(sorted(_PROJECT_STATUSES))
            raise ValueError(f"project status must be one of: {allowed}")


@dataclass(frozen=True, slots=True)
class ItemReference:
    id: str
    name: str

    def __post_init__(self) -> None:
        _identifier(self.id, "item id")
        _single_line(self.name, "item name")


@dataclass(frozen=True, slots=True)
class ProjectRecord:
    metadata: ProjectMetadata
    items: tuple[ItemReference, ...] = ()


@dataclass(frozen=True, slots=True)
class ItemState:
    id: str
    name: str
    status: str
    goal: str
    completion_criteria: str
    current_progress: str
    next_step: str
    blocker: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.id, "item id")
        _single_line(self.name, "item name")
        _single_line(self.status, "item status")
        if self.status not in _ITEM_STATUSES:
            allowed = ", ".join(sorted(_ITEM_STATUSES))
            raise ValueError(f"item status must be one of: {allowed}")
        _text(self.goal, "item goal")
        _text(self.completion_criteria, "item completion criteria")
        _text(self.current_progress, "item current progress")
        _text(self.next_step, "item next step")
        if self.blocker is not None:
            _text(self.blocker, "item blocker")


@dataclass(frozen=True, slots=True)
class ItemUpdate:
    """Absolute new values for selected Item fields."""

    values: Mapping[str, str | None]

    def __post_init__(self) -> None:
        copied = dict(self.values)
        if not copied:
            raise ValueError("item update must contain at least one field")
        unknown = copied.keys() - set(_ITEM_UPDATE_FIELDS)
        if unknown:
            names = ", ".join(sorted(unknown))
            raise ValueError(f"unknown item update fields: {names}")

        normalized: dict[str, str | None] = {}
        for field in _ITEM_UPDATE_FIELDS:
            if field not in copied:
                continue
            value = copied[field]
            if field == "blocker" and value is None:
                normalized[field] = None
                continue
            if not isinstance(value, str):
                raise ValueError(f"item update {field} must be a string")
            if field == "status":
                status = _single_line(value, "item update status")
                if status not in _ITEM_STATUSES:
                    allowed = ", ".join(sorted(_ITEM_STATUSES))
                    raise ValueError(f"item update status must be one of: {allowed}")
                normalized[field] = status
            else:
                normalized[field] = _text(value, f"item update {field}")

        object.__setattr__(self, "values", MappingProxyType(normalized))

    def to_dict(self) -> dict[str, str | None]:
        return dict(self.values)


@dataclass(frozen=True, slots=True)
class OrganizedTurn:
    """Organizer output required by the deterministic storage boundary."""

    project_id: str
    item_id: str
    summary: str
    updates: ItemUpdate

    def __post_init__(self) -> None:
        _identifier(self.project_id, "project id")
        _identifier(self.item_id, "item id")
        _text(self.summary, "event summary")


@dataclass(frozen=True, slots=True)
class ApplyResult:
    event_id: str
    appended: bool
    item: ItemState


@dataclass(frozen=True, slots=True)
class _EventLocation:
    project_id: str
    item_id: str
    block: str


class MarkdownStore:
    """Own the V1 Markdown layout and event-first state update sequence."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.projects_root = self.data_root / "projects"
        self.projects_root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def create_project(self, metadata: ProjectMetadata) -> ProjectRecord:
        with self._lock:
            project_dir = self._project_dir(metadata.id)
            project_file = project_dir / "project.md"
            if project_dir.exists():
                if project_file.is_file():
                    existing = self.read_project(metadata.id)
                    if existing.metadata == metadata:
                        return existing
                raise StorageConflictError(f"project already exists: {metadata.id}")

            (project_dir / "items").mkdir(parents=True)
            self._write_project(metadata, ())
            return ProjectRecord(metadata=metadata)

    def ensure_project(self, metadata: ProjectMetadata) -> ProjectRecord:
        """Create a Project or update its small metadata cover in place."""

        with self._lock:
            try:
                existing = self.read_project(metadata.id)
            except StorageNotFoundError:
                return self.create_project(metadata)
            self._write_project(metadata, existing.items)
            return ProjectRecord(metadata=metadata, items=existing.items)

    def list_projects(self) -> tuple[ProjectMetadata, ...]:
        projects: list[ProjectMetadata] = []
        for path in sorted(self.projects_root.glob("*/project.md")):
            projects.append(_parse_project(path.read_text(encoding="utf-8")).metadata)
        return tuple(projects)

    def read_project(self, project_id: str) -> ProjectRecord:
        path = self._project_file(project_id)
        if not path.is_file():
            raise StorageNotFoundError(f"project not found: {project_id}")
        record = _parse_project(path.read_text(encoding="utf-8"))
        if record.metadata.id != project_id:
            raise StorageFormatError(
                f"project id in {path} does not match directory name"
            )
        return record

    def create_item(self, project_id: str, item: ItemState) -> ItemState:
        with self._lock:
            project = self.read_project(project_id)
            item_dir = self._item_dir(project_id, item.id)
            item_file = item_dir / "item.md"

            if item_dir.exists():
                if item_file.is_file():
                    existing = self.read_item(project_id, item.id)
                    if existing == item:
                        events_file = item_dir / "events.md"
                        if not events_file.exists():
                            _atomic_write(events_file, "# Events\n")
                        self._rebuild_project_index(project.metadata)
                        return existing
                raise StorageConflictError(
                    f"item already exists: {project_id}/{item.id}"
                )

            item_dir.mkdir(parents=True)
            self._write_item(project_id, item)
            _atomic_write(item_dir / "events.md", "# Events\n")
            self._rebuild_project_index(project.metadata)
            return item

    def ensure_item(self, project_id: str, item: ItemState) -> ItemState:
        """Create a missing Item without overwriting an existing current state."""

        with self._lock:
            try:
                return self.read_item(project_id, item.id)
            except StorageNotFoundError:
                return self.create_item(project_id, item)

    def read_item(self, project_id: str, item_id: str) -> ItemState:
        path = self._item_file(project_id, item_id)
        if not path.is_file():
            raise StorageNotFoundError(f"item not found: {project_id}/{item_id}")
        item = _parse_item(path.read_text(encoding="utf-8"))
        if item.id != item_id:
            raise StorageFormatError(f"item id in {path} does not match directory name")
        return item

    def read_events(self, project_id: str, item_id: str) -> str:
        path = self._events_file(project_id, item_id)
        if not path.is_file():
            raise StorageNotFoundError(
                f"event log not found: {project_id}/{item_id}"
            )
        return path.read_text(encoding="utf-8")

    def apply_turn(
        self,
        turn: TurnCompleted,
        organized: OrganizedTurn,
    ) -> ApplyResult:
        """Append one QA Event, then idempotently update its target Item."""

        with self._lock:
            item = self.read_item(organized.project_id, organized.item_id)
            event_id = event_id_for_turn(turn)
            existing = self._find_event(event_id)

            if existing is not None:
                if (
                    existing.project_id != organized.project_id
                    or existing.item_id != organized.item_id
                ):
                    raise StorageConflictError(
                        "turn was already assigned to "
                        f"{existing.project_id}/{existing.item_id}"
                    )
                updates = _updates_from_event(existing.block, turn, event_id)
                appended = False
            else:
                updates = organized.updates
                block = _render_event(event_id, turn, organized.summary, updates)
                # Validate the state transition before the immutable Event is written.
                _apply_item_update(item, updates)
                self._append_event(organized.project_id, organized.item_id, block)
                appended = True

            updated_item = _apply_item_update(item, updates)
            self._write_item(organized.project_id, updated_item)
            return ApplyResult(
                event_id=event_id,
                appended=appended,
                item=updated_item,
            )

    def _append_event(self, project_id: str, item_id: str, block: str) -> None:
        path = self._events_file(project_id, item_id)
        existing = path.read_text(encoding="utf-8") if path.exists() else "# Events\n"
        updated = f"{existing.rstrip()}\n\n{block.rstrip()}\n"
        _atomic_write(path, updated)

    def _find_event(self, event_id: str) -> _EventLocation | None:
        marker = f"<!-- event:start {event_id} -->"
        found: _EventLocation | None = None
        for path in sorted(self.projects_root.glob("*/items/*/events.md")):
            text = path.read_text(encoding="utf-8")
            if marker not in text:
                continue
            block = _extract_event_block(text, event_id)
            location = _EventLocation(
                project_id=path.parents[2].name,
                item_id=path.parent.name,
                block=block,
            )
            if found is not None:
                raise StorageConflictError(f"duplicate event id found: {event_id}")
            found = location
        return found

    def _rebuild_project_index(self, metadata: ProjectMetadata) -> None:
        item_root = self._project_dir(metadata.id) / "items"
        references = tuple(
            ItemReference(item.id, item.name)
            for item in (
                _parse_item(path.read_text(encoding="utf-8"))
                for path in sorted(item_root.glob("*/item.md"))
            )
        )
        self._write_project(metadata, references)

    def _write_project(
        self,
        metadata: ProjectMetadata,
        items: tuple[ItemReference, ...],
    ) -> None:
        _atomic_write(self._project_file(metadata.id), _render_project(metadata, items))

    def _write_item(self, project_id: str, item: ItemState) -> None:
        _atomic_write(self._item_file(project_id, item.id), _render_item(item))

    def _project_dir(self, project_id: str) -> Path:
        return self.projects_root / _identifier(project_id, "project id")

    def _project_file(self, project_id: str) -> Path:
        return self._project_dir(project_id) / "project.md"

    def _item_dir(self, project_id: str, item_id: str) -> Path:
        return (
            self._project_dir(project_id)
            / "items"
            / _identifier(item_id, "item id")
        )

    def _item_file(self, project_id: str, item_id: str) -> Path:
        return self._item_dir(project_id, item_id) / "item.md"

    def _events_file(self, project_id: str, item_id: str) -> Path:
        return self._item_dir(project_id, item_id) / "events.md"


def _render_project(
    metadata: ProjectMetadata,
    items: tuple[ItemReference, ...],
) -> str:
    lines = [
        "---",
        f"id: {_yaml_scalar(metadata.id)}",
        f"name: {_yaml_scalar(metadata.name)}",
        f"summary: {_yaml_scalar(metadata.summary)}",
        f"status: {_yaml_scalar(metadata.status)}",
        "---",
        "",
        f"# {metadata.name}",
        "",
        "## Items",
    ]
    if not items:
        lines.extend(["", "暂无 Item。"])
    else:
        for item in sorted(items, key=lambda value: value.id):
            lines.extend(
                [
                    "",
                    f"- `{item.id}`：{item.name}",
                    f"  - 最新状态：[item.md](items/{item.id}/item.md)",
                    f"  - 历史记录：[events.md](items/{item.id}/events.md)",
                ]
            )
    return "\n".join(lines).rstrip() + "\n"


def _parse_project(text: str) -> ProjectRecord:
    frontmatter, body = _parse_frontmatter(text)
    _require_frontmatter_keys(
        frontmatter,
        required={"id", "name", "summary", "status"},
    )
    try:
        metadata = ProjectMetadata(
            id=_frontmatter_text(frontmatter, "id"),
            name=_frontmatter_text(frontmatter, "name"),
            summary=_frontmatter_text(frontmatter, "summary"),
            status=_frontmatter_text(frontmatter, "status"),
        )
    except ValueError as exc:
        raise StorageFormatError(str(exc)) from exc

    item_pattern = re.compile(r"^- `(?P<id>[^`]+)`：(?P<name>.+)$", re.MULTILINE)
    try:
        items = tuple(
            ItemReference(match.group("id"), match.group("name").strip())
            for match in item_pattern.finditer(body)
        )
    except ValueError as exc:
        raise StorageFormatError(str(exc)) from exc
    return ProjectRecord(metadata=metadata, items=items)


def _render_item(item: ItemState) -> str:
    lines = [
        "---",
        f"id: {_yaml_scalar(item.id)}",
        f"name: {_yaml_scalar(item.name)}",
        f"status: {_yaml_scalar(item.status)}",
        "---",
        "",
        f"# {item.name}",
        "",
        "## 目标",
        "",
        item.goal,
        "",
        "## 完成标准",
        "",
        item.completion_criteria,
        "",
        "## 当前进展",
        "",
        item.current_progress,
        "",
        "## 下一步",
        "",
        item.next_step,
    ]
    if item.blocker is not None:
        lines.extend(["", "## 阻塞", "", item.blocker])
    return "\n".join(lines).rstrip() + "\n"


def _parse_item(text: str) -> ItemState:
    frontmatter, body = _parse_frontmatter(text)
    _require_frontmatter_keys(frontmatter, required={"id", "name", "status"})
    sections = _parse_item_sections(body)
    missing = {
        "goal",
        "completion_criteria",
        "current_progress",
        "next_step",
    } - sections.keys()
    if missing:
        raise StorageFormatError(
            f"item body is missing sections: {', '.join(sorted(missing))}"
        )
    try:
        return ItemState(
            id=_frontmatter_text(frontmatter, "id"),
            name=_frontmatter_text(frontmatter, "name"),
            status=_frontmatter_text(frontmatter, "status"),
            goal=sections["goal"],
            completion_criteria=sections["completion_criteria"],
            current_progress=sections["current_progress"],
            next_step=sections["next_step"],
            blocker=sections.get("blocker"),
        )
    except ValueError as exc:
        raise StorageFormatError(str(exc)) from exc


def _parse_item_sections(body: str) -> dict[str, str]:
    names = "|".join(re.escape(name) for name in _ITEM_SECTION_NAMES)
    pattern = re.compile(
        rf"^## (?P<name>{names})[ \t]*$\n"
        rf"(?P<content>.*?)(?=^## (?:{names})[ \t]*$|\Z)",
        re.MULTILINE | re.DOTALL,
    )
    sections: dict[str, str] = {}
    for match in pattern.finditer(body):
        key = _ITEM_SECTION_NAMES[match.group("name")]
        content = match.group("content").strip()
        if not content:
            raise StorageFormatError(f"item section is empty: {match.group('name')}")
        sections[key] = content
    return sections


def event_id_for_turn(turn: TurnCompleted) -> str:
    """Return the stable Event id for one Harness Session/Turn identity."""

    source_key = "\0".join(
        (turn.platform, turn.session_id, turn.turn_id)
    ).encode("utf-8")
    return f"event-{hashlib.sha256(source_key).hexdigest()[:24]}"


def _render_event(
    event_id: str,
    turn: TurnCompleted,
    summary: str,
    updates: ItemUpdate,
) -> str:
    payload = turn.to_payload()
    lines = [
        f"<!-- event:start {event_id} -->",
        "",
        f"## {event_id}",
        "",
        "```yaml",
        f"id: {_yaml_scalar(event_id)}",
        f"completed_at: {_yaml_scalar(payload['completed_at'])}",
        "source:",
        f"  platform: {_yaml_scalar(turn.platform)}",
        f"  session_id: {_yaml_scalar(turn.session_id)}",
        f"  turn_id: {_yaml_scalar(turn.turn_id)}",
        f"source_suggestion_id: {_yaml_scalar(turn.source_suggestion_id)}",
        f"summary: {_yaml_scalar(summary)}",
        "updates:",
    ]
    for field in _ITEM_UPDATE_FIELDS:
        if field in updates.values:
            lines.append(f"  {field}: {_yaml_scalar(updates.values[field])}")
    lines.extend(
        [
            "```",
            "",
            "### 原始问题",
            "",
            turn.user_question,
            "",
            "### 原始回答",
            "",
            turn.final_answer,
        ]
    )
    if turn.tool_execution_evidence:
        lines.extend(
            [
                "",
                "### 工具执行证据",
                "",
                "```json",
                json.dumps(
                    [item.to_payload() for item in turn.tool_execution_evidence],
                    ensure_ascii=False,
                    indent=2,
                ),
                "```",
            ]
        )
    lines.extend(["", f"<!-- event:end {event_id} -->"])
    return "\n".join(lines)


def _extract_event_block(text: str, event_id: str) -> str:
    start_marker = f"<!-- event:start {event_id} -->"
    end_marker = f"<!-- event:end {event_id} -->"
    start = text.find(start_marker)
    if start < 0:
        raise StorageFormatError(f"event start marker not found: {event_id}")
    end = text.find(end_marker, start + len(start_marker))
    if end < 0:
        raise StorageFormatError(f"event end marker not found: {event_id}")
    return text[start : end + len(end_marker)]


def _updates_from_event(
    block: str,
    turn: TurnCompleted,
    expected_event_id: str,
) -> ItemUpdate:
    metadata = _parse_event_metadata(block)
    if metadata.get("id") != expected_event_id:
        raise StorageFormatError(f"event metadata id mismatch: {expected_event_id}")
    source = metadata.get("source")
    if not isinstance(source, Mapping):
        raise StorageFormatError("event source must be a mapping")
    expected_source = {
        "platform": turn.platform,
        "session_id": turn.session_id,
        "turn_id": turn.turn_id,
    }
    if dict(source) != expected_source:
        raise StorageConflictError(f"event source conflict: {expected_event_id}")
    updates = metadata.get("updates")
    if not isinstance(updates, Mapping):
        raise StorageFormatError("event updates must be a mapping")
    try:
        return ItemUpdate(updates)
    except ValueError as exc:
        raise StorageFormatError(str(exc)) from exc


def _parse_event_metadata(block: str) -> dict[str, Any]:
    match = re.search(r"```yaml\r?\n(?P<yaml>.*?)\r?\n```", block, re.DOTALL)
    if match is None:
        raise StorageFormatError("event YAML block not found")

    result: dict[str, Any] = {}
    section: str | None = None
    for raw_line in match.group("yaml").splitlines():
        if not raw_line.strip():
            continue
        if raw_line.startswith("  "):
            if section is None or not isinstance(result.get(section), dict):
                raise StorageFormatError("unexpected nested Event YAML field")
            key, value = _split_yaml_line(raw_line.strip())
            result[section][key] = _parse_yaml_scalar(value)
            continue
        key, value = _split_yaml_line(raw_line)
        if value:
            result[key] = _parse_yaml_scalar(value)
            section = None
        else:
            result[key] = {}
            section = key
    return result


def _apply_item_update(item: ItemState, update: ItemUpdate) -> ItemState:
    changes = update.to_dict()
    try:
        return replace(item, **changes)
    except (TypeError, ValueError) as exc:
        raise StorageFormatError(f"invalid item update: {exc}") from exc


def _parse_frontmatter(text: str) -> tuple[dict[str, Any], str]:
    match = re.match(r"\A---\r?\n(?P<yaml>.*?)\r?\n---\r?\n?", text, re.DOTALL)
    if match is None:
        raise StorageFormatError("Markdown file is missing YAML frontmatter")
    values: dict[str, Any] = {}
    for raw_line in match.group("yaml").splitlines():
        if not raw_line.strip():
            continue
        key, value = _split_yaml_line(raw_line)
        if not value:
            raise StorageFormatError(f"frontmatter field has no value: {key}")
        if key in values:
            raise StorageFormatError(f"duplicate frontmatter field: {key}")
        values[key] = _parse_yaml_scalar(value)
    return values, text[match.end() :]


def _split_yaml_line(line: str) -> tuple[str, str]:
    if ":" not in line:
        raise StorageFormatError(f"invalid YAML line: {line}")
    key, value = line.split(":", 1)
    key = key.strip()
    if not key:
        raise StorageFormatError("YAML field name cannot be empty")
    return key, value.strip()


def _yaml_scalar(value: str | None) -> str:
    if value is None:
        return "null"
    return json.dumps(value, ensure_ascii=False)


def _parse_yaml_scalar(value: str) -> str | None:
    if value == "null":
        return None
    if value.startswith('"'):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError as exc:
            raise StorageFormatError(f"invalid quoted YAML scalar: {value}") from exc
        if not isinstance(parsed, str):
            raise StorageFormatError("managed YAML scalars must be strings or null")
        return parsed
    return value


def _require_frontmatter_keys(
    values: Mapping[str, Any],
    *,
    required: set[str],
) -> None:
    missing = required - values.keys()
    if missing:
        raise StorageFormatError(
            f"missing frontmatter fields: {', '.join(sorted(missing))}"
        )
    unknown = values.keys() - required
    if unknown:
        raise StorageFormatError(
            f"unknown frontmatter fields: {', '.join(sorted(unknown))}"
        )


def _frontmatter_text(values: Mapping[str, Any], field: str) -> str:
    value = values[field]
    if not isinstance(value, str):
        raise StorageFormatError(f"frontmatter {field} must be a string")
    return value


def _atomic_write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        text=True,
    )
    temporary_path = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary_path, path)
    except BaseException:
        temporary_path.unlink(missing_ok=True)
        raise
