#!/usr/bin/env python3
"""Development-only compatibility wrapper for the packaged setup command.

The distributable interface is ``proactive-memory-service setup``.  This
wrapper remains for source-checkout demos and older runbooks; it deliberately
does not copy ``SKILL.md``.  Skill installation belongs to the Harness Skill
manager, while this command installs only the executable Hermes Connector.
"""

from __future__ import annotations

import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    repository = Path(__file__).resolve().parents[1]
    service_root = repository / "src"
    if str(service_root) not in sys.path:
        sys.path.insert(0, str(service_root))

    from proactive_memory_service.__main__ import main as service_main

    arguments = ["setup", "--harness", "hermes"]
    arguments.extend(sys.argv[1:] if argv is None else argv)
    service_main(arguments)


if __name__ == "__main__":
    main()
