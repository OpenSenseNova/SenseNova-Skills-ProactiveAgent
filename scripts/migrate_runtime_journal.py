#!/usr/bin/env python3
"""Migrate legacy Markdown runtime journals to JSONL."""

from __future__ import annotations

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from typing import Any


_RECORD_ID = re.compile(r"[a-z0-9][a-z0-9._-]{0,191}\Z")
_BLOCK = re.compile(
    r"<!-- runtime:start (?P<id>[a-z0-9][a-z0-9._-]{0,191}) -->\s*"
    r"## .*?\n\s*```json\n(?P<json>.*?)\n```\s*"
    r"<!-- runtime:end (?P=id) -->",
    re.DOTALL,
)


def convert_markdown(path: Path) -> list[str]:
    """Validate one legacy journal and return normalized JSONL lines."""

    text = path.read_text(encoding="utf-8")
    lines: list[str] = []
    cursor = 0
    seen_ids: set[str] = set()
    for match in _BLOCK.finditer(text):
        outside = text[cursor : match.start()].strip()
        if outside and outside != "# Runtime Journal":
            raise ValueError(f"unexpected text outside runtime record: {path}")
        cursor = match.end()
        record_id = match.group("id")
        if record_id in seen_ids:
            raise ValueError(f"duplicate runtime record id {record_id}: {path}")
        try:
            raw = json.loads(match.group("json"))
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid JSON in {path}: {record_id}") from exc
        if not isinstance(raw, dict) or raw.get("record_id") != record_id:
            raise ValueError(f"record id mismatch in {path}: {record_id}")
        kind = raw.get("kind")
        recorded_at = raw.get("recorded_at")
        payload = raw.get("payload")
        if (
            not isinstance(kind, str)
            or not kind.strip()
            or not isinstance(recorded_at, str)
            or not recorded_at.strip()
            or not isinstance(payload, dict)
            or not _RECORD_ID.fullmatch(record_id)
        ):
            raise ValueError(f"invalid runtime record in {path}: {record_id}")
        body: dict[str, Any] = {
            "record_id": record_id,
            "kind": kind,
            "recorded_at": recorded_at,
            "payload": payload,
        }
        lines.append(json.dumps(body, ensure_ascii=False, separators=(",", ":")))
        seen_ids.add(record_id)
    if text[cursor:].strip():
        raise ValueError(f"unexpected trailing text in runtime journal: {path}")
    return lines


def migrate(path: Path, *, remove_legacy: bool) -> Path:
    if path.name != "runtime.md":
        raise ValueError(f"expected a runtime.md file: {path}")
    target = path.with_name("runtime.jsonl")
    if target.exists():
        raise FileExistsError(f"target already exists: {target}")
    lines = convert_markdown(path)
    _atomic_write(target, "" if not lines else "\n".join(lines) + "\n")
    if remove_legacy:
        path.unlink()
    return target


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "paths",
        nargs="*",
        type=Path,
        help="specific runtime.md files; omit to scan --root",
    )
    parser.add_argument(
        "--root",
        type=Path,
        default=Path("data"),
        help="root to scan when no paths are supplied (default: data)",
    )
    parser.add_argument(
        "--remove-legacy",
        action="store_true",
        help="remove each runtime.md after its JSONL replacement is written",
    )
    args = parser.parse_args()
    paths = args.paths or sorted(args.root.rglob("runtime.md"))
    if not paths:
        raise SystemExit("no runtime.md files found")
    for path in paths:
        target = migrate(path, remove_legacy=args.remove_legacy)
        print(f"{path} -> {target}")


if __name__ == "__main__":
    main()
