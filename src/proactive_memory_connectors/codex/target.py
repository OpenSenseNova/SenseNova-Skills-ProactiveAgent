"""Launch configuration for the Codex app-server process."""

from __future__ import annotations

import shutil
from dataclasses import dataclass
from pathlib import Path

from .transport import CodexAppServerTransport


@dataclass(frozen=True, slots=True)
class CodexAppServerTarget:
    executable: str | Path = "codex"

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), "app-server")

    @classmethod
    def discover(cls) -> CodexAppServerTarget | None:
        executable = shutil.which("codex")
        return None if executable is None else cls(executable)

    def transport(
        self,
        cwd: str | Path | None = None,
    ) -> CodexAppServerTransport:
        return CodexAppServerTransport(self.command, cwd=cwd)

