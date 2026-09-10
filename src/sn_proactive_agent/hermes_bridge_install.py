"""Version-gated, reversible Web-only wiring for the Hermes TUI.

No downloads, git operations or model calls. Unknown/modified sources fail
closed before writes. The user must explicitly select the Hermes source root.
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .packaged_resources import connector_resource

SUPPORTED_REVISION = "b03c94dbed5ee72e97eace2376e02092cc854f6a"
SOURCE_HASH = "c7f8934f25d30c7b41b13853d2d13f2e9759ce5a26c36340c7073e1e00df6137"
BUILD_HASH = "152e99169e36953638ac09f86e7cd0e1ce73620af3568a3085d2bf732a0cc76e"
TARGETS = ("src/app/useMainApp.ts", "src/app/proactiveAgentWeb.ts", "dist/entry.js")
STATE_DIR = ".sn-proactive-agent-bridge"
IMPORT_ANCHOR = "import { useSubmission } from './useSubmission.js'\n"
HOOK_ANCHOR = "  // Drain one queued message whenever the session settles (busy → false):\n"
HOOK = """  // Proactive Agent: Web owns decisions; this only submits approved input.
  useEffect(() => {
    const sessionId = ui.sid
    if (!sessionId) return
    return startWebAgentBridge({
      sessionId,
      canSubmit: () => getUiState().sid === sessionId && !getUiState().busy,
      submit: async text => {
        try {
          await submitApprovedInput(sessionId, text, {
            currentSessionId: () => getUiState().sid,
            isBusy: () => getUiState().busy,
            showSubmission: value => {
              turnController.clearStatusTimer()
              setLastUserMsg(value)
              appendMessage({ role: 'user', text: value })
              patchUiState({ busy: true, status: 'running…' })
              turnController.bufRef = ''
              turnController.interrupted = false
            },
            promptSubmit: params => gw.request('prompt.submit', params)
          })
        } catch (error) {
          if (getUiState().sid === sessionId) patchUiState({ busy: false, status: 'ready' })
          throw error
        }
      },
      reportError: text => sys(`Proactive Agent: ${text}`)
    })
  }, [ui.sid, sys, gw, appendMessage])

"""


class BridgeInstallError(RuntimeError):
    pass


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def instance_id(home: Path) -> str:
    return digest(str(home.expanduser().resolve()).encode("utf-8"))


def _base(root: Path) -> Path:
    base = root.expanduser().resolve() / "ui-tui"
    if not base.is_dir() or base.is_symlink():
        raise BridgeInstallError("Hermes root must contain a regular ui-tui directory")
    for relative in (*TARGETS, "scripts/build.mjs", STATE_DIR, ".proactive-memory-bridge/install.json"):
        path = base / relative
        for part in (path, *path.parents):
            if part == base:
                break
            if part.is_symlink():
                raise BridgeInstallError(f"Refusing symlink target: {path}")
    return base


def _check_legacy_bridge(base: Path) -> None:
    record_dir = base / ".proactive-memory-bridge"
    if not record_dir.exists():
        return
    try:
        record = json.loads((record_dir / "install.json").read_text(encoding="utf-8"))
        legacy_targets = {"src/app/useMainApp.ts", "src/app/proactiveMemoryWeb.ts", "dist/entry.js"}
        removed = (
            isinstance(record, dict) and record.get("schema") == 1
            and record.get("status") == "removed"
            and isinstance(record.get("original"), dict)
            and set(record["original"]) == legacy_targets
        )
    except (OSError, ValueError):
        removed = False
    if not removed:
        raise BridgeInstallError(
            "Legacy proactive-memory bridge detected. Restore it with the old "
            "runtime's uninstall command before installing sn-proactive-agent. "
            "An incomplete record requires manual review. No files changed."
        )


def _write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, name = tempfile.mkstemp(prefix=".snpa-", dir=path.parent)
    temporary = Path(name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists():
            temporary.chmod(path.stat().st_mode & 0o777)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _state(base: Path) -> dict | None:
    path = base / STATE_DIR / "install.json"
    if not path.exists():
        return None
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        if state["schema"] != 1 or set(state["original"]) != set(TARGETS):
            raise ValueError("invalid manifest")
        return state
    except (ValueError, KeyError, TypeError) as exc:
        raise BridgeInstallError("Invalid bridge installation record; manual review required") from exc


def inspect_install(root: Path) -> tuple[bool, str]:
    try:
        base = _base(root)
        state = _state(base)
        if not state or state.get("status") != "installed":
            return False, "No completed Web-only bridge installation"
        if set(state.get("installed", {})) != set(TARGETS):
            return False, "Incomplete bridge installation record"
        for relative in TARGETS:
            target = base / relative
            if not target.is_file() or digest(target.read_bytes()) != state["installed"][relative]:
                return False, f"Bridge file changed: {relative}"
        return True, "Installed source and built TUI match the installation record (not a live probe)"
    except (OSError, BridgeInstallError) as exc:
        return False, str(exc)


def _build(base: Path) -> None:
    node = shutil.which("node")
    if not node:
        raise BridgeInstallError("Node.js 22+ is required; no dependencies were installed")
    version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10)
    if version.returncode or not version.stdout.strip().lstrip("v").split(".")[0].isdigit():
        raise BridgeInstallError("Cannot verify Node.js version")
    if int(version.stdout.strip().lstrip("v").split(".")[0]) < 22:
        raise BridgeInstallError("Node.js 22+ is required")
    if not any((parent / "node_modules/esbuild").is_dir() for parent in (base, base.parent)):
        raise BridgeInstallError("Missing Hermes build dependencies; run npm ci in the Hermes root after approval")
    result = subprocess.run(
        [node, "scripts/build.mjs"], cwd=base, capture_output=True, text=True, timeout=60,
    )
    if result.returncode or not (base / "dist/entry.js").is_file():
        raise BridgeInstallError("Hermes TUI build failed; original files will be restored")


@contextmanager
def install_bridge(
    root: Path, home: Path, *, dry_run: bool = False,
    builder: Callable[[Path], None] | None = None,
) -> Iterator[Path]:
    base = _base(root)
    _check_legacy_bridge(base)
    old = _state(base)
    if old and old.get("status") == "installed":
        ok, reason = inspect_install(root)
        if not ok or old.get("instance_id") != instance_id(home):
            raise BridgeInstallError(reason if not ok else "Bridge belongs to another Hermes home")
        bundled = connector_resource("hermes/tui/web_bridge.ts").read_bytes()
        if digest(bundled) != old["installed"][TARGETS[1]]:
            raise BridgeInstallError("Bridge version changed; uninstall the old bridge before upgrading")
        yield base
        return
    source = base / TARGETS[0]
    build_file = base / "scripts/build.mjs"
    if digest(source.read_bytes()) != SOURCE_HASH or digest(build_file.read_bytes()) != BUILD_HASH:
        raise BridgeInstallError(
            f"Unsupported or modified Hermes TUI; supported baseline: {SUPPORTED_REVISION}. No files changed."
        )
    if (base / TARGETS[1]).exists():
        raise BridgeInstallError("Bridge target already exists without a matching installation record")
    text = source.read_text(encoding="utf-8")
    if text.count(IMPORT_ANCHOR) != 1 or text.count(HOOK_ANCHOR) != 1:
        raise BridgeInstallError("Hermes submission entry is incompatible")
    text = text.replace(IMPORT_ANCHOR, IMPORT_ANCHOR + "import { startWebAgentBridge, submitApprovedInput } from './proactiveAgentWeb.js'\n")
    text = text.replace(HOOK_ANCHOR, HOOK + HOOK_ANCHOR)
    if dry_run:
        yield base
        return
    record_dir = base / STATE_DIR
    if record_dir.exists():
        if not old or old.get("status") != "removed":
            raise BridgeInstallError("Previous installation incomplete; review backups before retrying")
        archive = Path(tempfile.mkdtemp(prefix=".snpa-backup-", dir=base))
        archive.rmdir()
        record_dir.rename(archive)
    record_dir.mkdir(mode=0o700)
    original = {}
    for index, relative in enumerate(TARGETS):
        path = base / relative
        raw = path.read_bytes() if path.is_file() else None
        original[relative] = {"sha256": digest(raw) if raw is not None else None, "mode": path.stat().st_mode & 0o777 if raw is not None else None}
        if raw is not None:
            _write(record_dir / f"{index}.bak", raw)
    state = {"schema": 1, "status": "installing", "instance_id": instance_id(home), "original": original}
    _write(record_dir / "install.json", json.dumps(state).encode())
    try:
        _write(source, text.encode())
        _write(base / TARGETS[1], connector_resource("hermes/tui/web_bridge.ts").read_bytes())
        (builder or _build)(base)
        state["installed"] = {relative: digest((base / relative).read_bytes()) for relative in TARGETS}
        yield base
        state["status"] = "installed"
        _write(record_dir / "install.json", json.dumps(state).encode())
    except BaseException:
        _restore(base, state)
        raise


def _restore(base: Path, state: dict) -> None:
    record_dir = base / STATE_DIR
    # Verify every backup before restoring anything.
    for index, relative in enumerate(TARGETS):
        expected = state["original"][relative]["sha256"]
        if expected is not None and digest((record_dir / f"{index}.bak").read_bytes()) != expected:
            raise BridgeInstallError("Backup mismatch; original files not restored automatically")
    for index, relative in enumerate(TARGETS):
        target = base / relative
        original = state["original"][relative]
        if original["sha256"] is None:
            target.unlink(missing_ok=True)
        else:
            _write(target, (record_dir / f"{index}.bak").read_bytes())
            target.chmod(original["mode"])
    state["status"] = "removed"
    _write(record_dir / "install.json", json.dumps(state).encode())


def uninstall_bridge(root: Path, *, home: Path | None = None, dry_run: bool = False) -> bool:
    base = _base(root)
    state = _state(base)
    if not state or state.get("status") == "removed":
        return False
    if home is not None and state.get("instance_id") != instance_id(home):
        raise BridgeInstallError("Bridge belongs to another Hermes home; no files changed")
    ok, reason = inspect_install(root)
    if not ok:
        raise BridgeInstallError(reason + "; refusing to overwrite later Hermes changes")
    if not dry_run:
        _restore(base, state)
    return True
