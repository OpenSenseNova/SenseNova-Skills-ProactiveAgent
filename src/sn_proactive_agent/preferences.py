"""Small persisted preferences shared by the local Web dashboard and Core."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path

SUPPORTED_OUTPUT_LANGUAGES = frozenset({"zh", "en"})


def normalize_output_language(value: object, *, default: str = "zh") -> str:
    """Normalize UI language values to the two prompt languages we support."""
    raw = str(value or "").strip().lower()
    if raw.startswith("zh"):
        return "zh"
    if raw.startswith("en"):
        return "en"
    return default if default in SUPPORTED_OUTPUT_LANGUAGES else "zh"


class PreferenceStore:
    """Persist only small, non-sensitive service preferences in data root."""

    def __init__(self, data_root: str | Path) -> None:
        self.data_root = Path(data_root)
        self.path = self.data_root / "preferences.json"
        self.data_root.mkdir(parents=True, exist_ok=True)

    def get_output_language(self) -> str:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return "zh"
        if not isinstance(payload, dict):
            return "zh"
        return normalize_output_language(payload.get("output_language"))

    def set_output_language(self, value: object) -> str:
        language = normalize_output_language(value)
        payload = {"output_language": language}
        fd, temporary = tempfile.mkstemp(prefix="preferences.", suffix=".tmp", dir=self.data_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(payload, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
        return language
