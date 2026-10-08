"""Small persisted registry for Harness observation settings."""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Mapping


HARNESS_LABELS = {
    "hermes": "Hermes",
    "openclaw": "OpenClaw",
    "codex": "Codex",
}


class HarnessRegistry:
    """Expose enabled/available/connected Harness state to the Web UI."""

    def __init__(
        self,
        data_root: str | Path,
        *,
        available: Mapping[str, bool] | None = None,
        connected: Mapping[str, bool] | None = None,
    ) -> None:
        self.data_root = Path(data_root)
        self.data_root.mkdir(parents=True, exist_ok=True)
        self.path = self.data_root / "harnesses.json"
        self.available = {
            harness_id: bool((available or {}).get(harness_id, False))
            for harness_id in HARNESS_LABELS
        }
        self.connected = {
            harness_id: bool((connected or {}).get(harness_id, False))
            for harness_id in HARNESS_LABELS
        }

    def snapshot(self) -> list[dict[str, object]]:
        saved = self._read()
        result: list[dict[str, object]] = []
        for harness_id, label in HARNESS_LABELS.items():
            is_available = self.available[harness_id]
            enabled = bool(saved.get(harness_id, is_available and harness_id == "hermes"))
            result.append(
                {
                    "id": harness_id,
                    "label": label,
                    "enabled": enabled,
                    "available": is_available,
                    "connected": bool(self.connected[harness_id] and enabled),
                    "status": self._status(harness_id, enabled, is_available),
                }
            )
        return result

    def set_enabled(self, harness_id: str, enabled: bool) -> dict[str, object]:
        if harness_id not in HARNESS_LABELS:
            raise KeyError(harness_id)
        if not isinstance(enabled, bool):
            raise ValueError("enabled must be boolean")
        if enabled and not self.available[harness_id]:
            raise RuntimeError(f"{HARNESS_LABELS[harness_id]} 尚未配置，无法启用监测")
        saved = self._read()
        saved[harness_id] = enabled
        self._write(saved)
        return next(item for item in self.snapshot() if item["id"] == harness_id)

    def allows_platform(self, platform: str) -> bool:
        """Return whether an inbound connector platform is being observed.

        Connector ids carry a surface suffix (for example ``hermes-cli`` or
        ``hermes-tui``), while the settings page uses the Harness id.  Keeping
        this mapping here prevents the HTTP boundary from knowing Harness
        naming details.
        """
        if not isinstance(platform, str) or not platform.strip():
            return False
        harness_id = platform.strip().split("-", 1)[0]
        if harness_id not in HARNESS_LABELS:
            return False
        return any(
            item["id"] == harness_id
            and bool(item["enabled"])
            and bool(item["available"])
            for item in self.snapshot()
        )

    def _status(self, harness_id: str, enabled: bool, available: bool) -> str:
        if not enabled:
            return "disabled"
        if not available:
            return "unavailable"
        if self.connected[harness_id]:
            return "connected"
        return "ready"

    def _read(self) -> dict[str, bool]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, OSError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {key: value for key, value in payload.items() if key in HARNESS_LABELS and isinstance(value, bool)}

    def _write(self, payload: Mapping[str, bool]) -> None:
        fd, temporary = tempfile.mkstemp(prefix="harnesses.", suffix=".tmp", dir=self.data_root)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(dict(payload), handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        finally:
            try:
                os.unlink(temporary)
            except FileNotFoundError:
                pass
