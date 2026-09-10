"""Canonical environment settings with read compatibility for existing installs."""

from __future__ import annotations

import os
from collections.abc import MutableMapping


def env_value(name: str, default: str | None = None) -> str | None:
    """Prefer the new name, including an explicitly empty value, over its alias."""

    legacy = name.replace("SN_PROACTIVE_AGENT_", "PROACTIVE_MEMORY_", 1)
    return os.environ.get(name, os.environ.get(legacy, default))


def set_compatible_env(environment: MutableMapping[str, str], name: str, value: str) -> None:
    """Keep already-installed Hermes hooks safe while upgrading the runtime."""

    environment[name] = value
    environment[name.replace("SN_PROACTIVE_AGENT_", "PROACTIVE_MEMORY_", 1)] = value
