"""Optional JsonReasoner backed by one-shot Codex App Server threads."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .client import CodexAppServerClient
from .target import CodexAppServerTarget


class CodexReasonerError(RuntimeError):
    """Raised when Codex cannot return one structured JSON object."""


class CodexJsonReasoner:
    """Use an isolated Codex thread for Organizer/Judge JSON decisions."""

    def __init__(
        self,
        executable: str | Path = "codex",
        *,
        cwd: str | Path,
        model: str | None = None,
    ) -> None:
        self.target = CodexAppServerTarget(executable)
        self.cwd = Path(cwd).expanduser().resolve()
        self.model = model

    def ask_json(self, prompt: str) -> Mapping[str, Any]:
        with self.target.transport(self.cwd) as transport:
            client = CodexAppServerClient(transport)
            client.initialize()
            thread = client.start_thread(
                self.cwd,
                model=self.model,
                approval_policy="never",
                sandbox_policy={"type": "readOnly"},
            )
            result = client.prompt(thread.thread_id, prompt)
        try:
            return _extract_json_object(result.final_answer)
        except (ValueError, json.JSONDecodeError) as exc:
            raise CodexReasonerError(
                "Codex semantic worker did not return one JSON object"
            ) from exc


def _extract_json_object(text: str) -> Mapping[str, Any]:
    candidate = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.S | re.I)
    if fenced:
        candidate = fenced.group(1)
    else:
        start, end = candidate.find("{"), candidate.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("no JSON object found")
        candidate = candidate[start : end + 1]
    value = json.loads(candidate)
    if not isinstance(value, Mapping):
        raise ValueError("JSON result must be an object")
    return value
