"""Locate connector files from an installed distribution.

The source checkout keeps Harness connectors next to the Core service under
``src/proactive_memory_connectors``.  A wheel exposes that directory under
the stable ``proactive_memory_connectors`` package namespace instead.  This
module is the one small boundary that installation/setup code should use; it
avoids assuming that the source repository is present on the user's machine.

The context manager returns ordinary :class:`~pathlib.Path` objects.  That is
important for Harness installers, which need to copy files into a user's
configuration directory.  ``importlib.resources.as_file`` also makes the
helper work when a package is loaded from an archive rather than an unpacked
wheel.
"""

from __future__ import annotations

from contextlib import ExitStack, contextmanager
from dataclasses import dataclass
from importlib.resources import as_file, files
from importlib.resources.abc import Traversable
from pathlib import Path
from typing import Iterator


CONNECTOR_PACKAGE = "proactive_memory_connectors"


@dataclass(frozen=True, slots=True)
class HermesConnectorResources:
    """Materialized files needed by the supported Hermes integration."""

    classic_hook: Path
    tui_module: Path | None
    tui_plugin: Path | None
    tui_patch: Path | None


def connector_resource(relative_path: str) -> Traversable:
    """Return one connector resource from the installed package.

    ``relative_path`` uses POSIX separators and is restricted to the connector
    tree.  During local development, before the wheel is installed, the
    original source-checkout directory is used as a compatibility fallback.
    """

    normalized = _normalize_relative_path(relative_path)
    try:
        root = files(CONNECTOR_PACKAGE)
    except ModuleNotFoundError:
        root = _checkout_connector_root()
    resource = root.joinpath(*normalized.split("/"))
    if not resource.is_file():
        raise FileNotFoundError(f"connector resource not found: {relative_path}")
    return resource


@contextmanager
def materialize_hermes_resources(
    *,
    include_tui: bool = True,
) -> Iterator[HermesConnectorResources]:
    """Yield local paths for Hermes hook/plugin resources.

    The yielded paths are valid until the context exits.  Callers should copy
    them while inside the context rather than retaining paths for later use.
    """

    with ExitStack() as stack:
        classic_hook = stack.enter_context(
            as_file(connector_resource("hermes/classic/hermes_hook.py"))
        )
        if include_tui:
            tui_module = stack.enter_context(
                as_file(connector_resource("hermes/tui/__init__.py"))
            )
            tui_plugin = stack.enter_context(
                as_file(connector_resource("hermes/tui/plugin.yaml"))
            )
            tui_patch = stack.enter_context(
                as_file(
                    connector_resource(
                        "hermes/tui/patches/hermes-tui-acp-submit.patch"
                    )
                )
            )
        else:
            tui_module = tui_plugin = tui_patch = None

        yield HermesConnectorResources(
            classic_hook=classic_hook,
            tui_module=tui_module,
            tui_plugin=tui_plugin,
            tui_patch=tui_patch,
        )


def packaged_resource_inventory() -> tuple[str, ...]:
    """Return the resource paths that the first Hermes package promises."""

    return (
        "hermes/classic/hermes_hook.py",
        "hermes/tui/__init__.py",
        "hermes/tui/plugin.yaml",
        "hermes/tui/web_bridge.ts",
        "hermes/tui/patches/hermes-tui-acp-submit.patch",
    )


def _normalize_relative_path(relative_path: str) -> str:
    if not isinstance(relative_path, str) or not relative_path.strip():
        raise ValueError("relative_path must be a non-empty string")
    normalized = relative_path.replace("\\", "/").strip("/")
    parts = normalized.split("/")
    if (
        not normalized
        or any(part in {"", ".", ".."} for part in parts)
        or not normalized.startswith("acp/")
        and not normalized.startswith("hermes/")
    ):
        raise ValueError("relative_path must stay inside the connector tree")
    return normalized


def _checkout_connector_root() -> Path:
    # .../src/proactive_memory_service/packaged_resources.py
    root = Path(__file__).resolve().parents[1] / "proactive_memory_connectors"
    if not root.is_dir():
        raise ModuleNotFoundError(
            f"{CONNECTOR_PACKAGE} is not installed and source connectors are absent"
        )
    return root


__all__ = [
    "CONNECTOR_PACKAGE",
    "HermesConnectorResources",
    "connector_resource",
    "materialize_hermes_resources",
    "packaged_resource_inventory",
]
