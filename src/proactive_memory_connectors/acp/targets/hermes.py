"""Launch configuration for using Hermes as a real ACP Agent."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

from ..transport import StdioJsonRpcTransport


@dataclass(frozen=True, slots=True)
class HermesAcpTarget:
    """Build an isolated ``hermes acp`` stdio transport.

    The ACP Client itself emits the V1 turn events, so Hermes shell hooks are
    disabled for this child process to avoid delivering the same turn twice.
    Provider/model credentials still come from the user's normal Hermes setup.
    """

    executable: str | Path
    safe_mode: bool = True

    @property
    def command(self) -> tuple[str, ...]:
        return (str(self.executable), "acp")

    def environment(
        self,
        base: Mapping[str, str] | None = None,
    ) -> dict[str, str]:
        environment = dict(os.environ if base is None else base)
        environment["PROACTIVE_MEMORY_INTERNAL_REASONER"] = "1"
        environment["HERMES_IGNORE_RULES"] = "1"
        if self.safe_mode:
            environment["HERMES_SAFE_MODE"] = "1"
        else:
            environment.pop("HERMES_SAFE_MODE", None)
        return environment

    def transport(self, cwd: str | Path) -> StdioJsonRpcTransport:
        return StdioJsonRpcTransport(
            self.command,
            cwd=Path(cwd).expanduser().resolve(),
            env=self.environment(),
        )
