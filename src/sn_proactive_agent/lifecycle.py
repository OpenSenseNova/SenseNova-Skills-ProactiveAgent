"""Installation and local lifecycle helpers for the Proactive Agent CLI.

The runtime package is deliberately independent from a source checkout.  This
module contains the small amount of filesystem/configuration work needed after
``pipx install``:

* ``doctor`` performs read-only checks;
* ``setup`` copies Hermes observation resources and merges its hooks safely;
* ``uninstall`` removes only the Connector that this package owns.

User state is never removed by this module.  It lives in
``~/.sn-proactive-agent`` and is intentionally outside the installation paths.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from contextlib import contextmanager
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable, TextIO

from .packaged_resources import connector_resource, materialize_hermes_resources
from .environment import env_value
from .hermes_bridge_install import BridgeInstallError, install_bridge, instance_id, inspect_install, uninstall_bridge


class LifecycleError(RuntimeError):
    """Raised when setup or cleanup cannot be performed safely."""


@dataclass(frozen=True, slots=True)
class DoctorCheck:
    """One read-only diagnostic result."""

    name: str
    ok: bool
    detail: str
    required: bool = True
    verified: bool = True

    @property
    def status(self) -> str:
        if not self.verified:
            return "unverified"
        return "passed" if self.ok else "failed"

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "ok": self.status == "passed",
            "detail": self.detail,
            "required": self.required,
            "status": self.status,
        }


@dataclass(frozen=True, slots=True)
class DoctorReport:
    """Stable, serialisable output from :func:`run_doctor`."""

    checks: tuple[DoctorCheck, ...]
    scope: str = "runtime"

    @property
    def ok(self) -> bool:
        return all(check.status == "passed" for check in self.checks if check.required)

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "scope": self.scope,
            "checks": [check.to_dict() for check in self.checks],
        }


@dataclass(frozen=True, slots=True)
class SetupResult:
    """Paths and warnings produced by a Harness setup operation."""

    harness: str
    hermes_home: Path
    hook_path: Path
    plugin_path: Path | None
    config_path: Path
    config_changed: bool
    warnings: tuple[str, ...] = ()
    bridge_root: Path | None = None


@dataclass(frozen=True, slots=True)
class UninstallResult:
    """Paths removed by a Harness cleanup operation."""

    harness: str
    hermes_home: Path
    removed: tuple[Path, ...]
    config_changed: bool
    data_root: Path


def default_data_root() -> Path:
    """Return the user data directory without creating it."""

    configured = env_value("SN_PROACTIVE_AGENT_DATA_ROOT", "").strip()
    if configured:
        return Path(configured).expanduser()
    current = Path.home() / ".sn-proactive-agent"
    legacy = Path.home() / ".proactive-memory"
    # Reuse existing state in place; never move, merge or create data here.
    if not current.exists() and legacy.is_dir():
        return legacy
    return current


def resolve_data_root(value: str | Path | None = None) -> Path:
    """Resolve a data-root argument using the documented user default."""

    return (Path(value).expanduser() if value is not None else default_data_root()).resolve()


def run_doctor(
    *,
    harness: str | None = None,
    hermes_executable: str | None = None,
    hermes_home: str | Path | None = None,
    hermes_root: str | Path | None = None,
    session_id: str | None = None,
    data_root: str | Path | None = None,
    service_url: str | None = None,
    check_service: bool = False,
    check_port: int | None = None,
) -> DoctorReport:
    """Perform read-only local checks.

    ``doctor`` intentionally does not create directories, start processes, or
    contact a service unless ``check_service`` (or an explicit URL) is passed.
    This makes it safe for an Agent to run before asking for installation
    authorization. Installed observer files do not prove that Hermes loaded
    them or that its live Session can resume. Until a capability probe exists,
    these required integration checks stay explicitly unverified.
    """

    checks: list[DoctorCheck] = []
    normalized = harness.strip().lower() if harness is not None else None
    minimum = (3, 11)
    current = sys.version_info[:2]
    checks.append(
        DoctorCheck(
            "python",
            current >= minimum,
            f"Python {current[0]}.{current[1]} (需要 >= {minimum[0]}.{minimum[1]}); {sys.executable}",
        )
    )

    package_root = _package_root()
    checks.append(
        DoctorCheck(
            "runtime_package",
            package_root.is_dir(),
            str(package_root),
        )
    )

    hook_available, hook_detail, plugin_available, plugin_detail = (
        _inspect_hermes_resources()
    )
    checks.append(
        DoctorCheck(
            "hermes_connector",
            hook_available,
            hook_detail,
            required=normalized in (None, "hermes"),
        )
    )
    checks.append(
        DoctorCheck(
            "hermes_tui_plugin",
            plugin_available,
            plugin_detail,
            required=normalized == "hermes",
        )
    )

    if harness is not None:
        supported = normalized == "hermes"
        checks.append(
            DoctorCheck(
                "harness",
                supported,
                "Hermes（提供接入资源，不代表当前实例已完成验收）"
                if supported else f"暂不支持 Harness: {harness}",
            )
        )
        if supported:
            executable = shutil.which(hermes_executable or "hermes")
            checks.append(
                DoctorCheck(
                    "hermes_executable",
                    bool(executable),
                    executable or "找不到可执行的 hermes；请检查指定路径或 PATH",
                )
            )
            home = Path(
                hermes_home
                if hermes_home is not None
                else os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")
            ).expanduser().resolve()
            checks.extend(_hermes_integration_checks(
                home, service_url=service_url, session_id=session_id, hermes_root=hermes_root,
            ))

    root = resolve_data_root(data_root)
    checks.append(
        DoctorCheck(
            "data_root",
            _directory_writable_or_creatable(root),
            f"{root}（不会由 doctor 创建）",
        )
    )

    if check_port is not None:
        checks.append(_port_check(check_port))

    if check_service or service_url:
        url = (service_url or "http://127.0.0.1:8080").rstrip("/") + "/health"
        checks.append(_service_check(url))

    return DoctorReport(tuple(checks), scope=normalized or "runtime")


def _hermes_integration_checks(
    home: Path, *, service_url: str | None = None, session_id: str | None = None,
    hermes_root: str | Path | None = None,
) -> tuple[DoctorCheck, ...]:
    """Inspect owned resources only; do not read config, secrets or transcripts.

    Static installation and live per-Session evidence are separate checks.
    No model call or example conversation is sent by the diagnostic itself.
    """

    checks = (
        _hermes_observer_check(home),
        DoctorCheck(
            "hermes_turn_capture",
            False,
            "需使用 --url 与 --session-id 检查目标窗口实际到达的完整 QA；doctor 不发送对话。",
            verified=False,
        ),
        DoctorCheck(
            "hermes_same_session_resume",
            False,
            "需使用 --url 与 --session-id 检查获批动作在原 Session 的执行回流；"
            "已安装或桥接在线不等于续跑已验证。",
            verified=False,
        ),
    )
    if hermes_root is not None:
        ok, detail = inspect_install(Path(hermes_root))
        checks += (DoctorCheck("hermes_bridge_installed", ok, detail),)
    if not service_url or not session_id:
        return checks
    from urllib.parse import urlencode
    query = urlencode({"platform": "hermes-tui", "session_id": session_id, "instance_id": instance_id(home)})
    request = urllib.request.Request(service_url.rstrip("/") + "/v1/bridge/diagnostics?" + query)
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            payload = json.loads(response.read(8192).decode("utf-8"))
        if (
            not isinstance(payload, dict) or payload.get("protocol") != 1
            or payload.get("platform") != "hermes-tui" or payload.get("session_id") != session_id
            or payload.get("instance_id") != instance_id(home)
        ):
            raise ValueError("diagnostic identity mismatch")
    except (OSError, ValueError, urllib.error.URLError):
        return checks + (DoctorCheck("hermes_bridge_online", False,
            "无法取得匹配实例和 Session 的诊断；旧服务、错误地址或离线均不能视为通过", verified=False),)
    online = payload.get("online") is True and payload.get("roundtrip") is True
    checks = tuple(c for c in checks if c.name not in {"hermes_turn_capture", "hermes_same_session_resume"})
    checks += (DoctorCheck("hermes_bridge_online", online,
        "桥接已完成在线往返探测" if online else "目标 Session 桥接离线或尚未完成探测"),)
    for name, field, description in (
        ("hermes_turn_capture", "turn_completed", "同一在线窗口已收到配对的 turn.started 与完整 QA"),
        ("hermes_same_session_resume", "resume_completed", "获批动作已在原 Session 提交并带来源建议 ID 回流"),
    ):
        value = payload.get(field)
        ok = online and isinstance(value, str) and bool(value.strip())
        checks += (DoctorCheck(name, ok, description if ok else "尚无当前在线窗口的实际回流证据", verified=ok),)
    return checks


def _hermes_observer_check(home: Path) -> DoctorCheck:
    plugin = home / "plugins" / "sn-proactive-agent-tui"
    name = "hermes_observer_installed"
    if plugin.is_symlink():
        return DoctorCheck(
            name, False, f"观测目录是符号链接，未验证目标内容：{plugin}", verified=False
        )
    missing: list[str] = []
    different: list[str] = []
    try:
        for filename in ("__init__.py", "plugin.yaml"):
            installed = plugin / filename
            if installed.is_symlink():
                return DoctorCheck(
                    name, False, f"观测文件是符号链接，未验证目标内容：{installed}",
                    verified=False,
                )
            if not installed.is_file():
                missing.append(filename)
                continue
            bundled = connector_resource(f"hermes/tui/{filename}")
            if installed.read_bytes() != bundled.read_bytes():
                different.append(filename)
    except (OSError, ModuleNotFoundError):
        return DoctorCheck(
            name, False, f"无法核对观测文件：{plugin}；检查权限和包内资源",
            verified=False,
        )
    if missing:
        return DoctorCheck(name, False, f"{plugin} 缺少：{', '.join(missing)}")
    if different:
        return DoctorCheck(
            name, False,
            f"{plugin} 与当前运行包不同：{', '.join(different)}；"
            "可能是旧版或本地定制，保留文件并先确认兼容性",
            verified=False,
        )
    return DoctorCheck(
        name, True, f"{plugin} 的观测文件与运行包一致；尚未证明已启用或可续跑"
    )


@contextmanager
def _observer_transaction(home: Path):
    """Back up owned files in memory; refuse overwriting unrecognised resources."""
    plugin = home / "plugins/sn-proactive-agent-tui"
    if plugin.is_symlink():
        raise LifecycleError("观测目录为符号链接，不能自动覆盖")
    files = {home / "config.yaml": None, plugin / "bridge-install.json": None}
    resources = {
        home / "skills/productivity/sn-proactive-agent/connectors/hermes/classic/hermes_hook.py": "hermes/classic/hermes_hook.py",
        plugin / "__init__.py": "hermes/tui/__init__.py",
        plugin / "plugin.yaml": "hermes/tui/plugin.yaml",
        plugin / "patches/hermes-tui-acp-submit.patch": "hermes/tui/patches/hermes-tui-acp-submit.patch",
    }
    files.update(resources)
    before = {}
    for path in files:
        for part in (path, *path.parents):
            if part == home:
                break
            if part.is_symlink():
                raise LifecycleError("目标配置、观测文件或上级目录为符号链接，不能自动覆盖")
        raw = path.read_bytes() if path.is_file() else None
        if path in resources and raw is not None and raw != connector_resource(resources[path]).read_bytes():
            raise LifecycleError(f"观测文件与当前包不同，保留并先确认迁移方式：{path}")
        before[path] = (raw, path.stat().st_mode & 0o777 if raw is not None else None)
    try:
        yield
    except BaseException:
        for path, (raw, mode) in before.items():
            if raw is None:
                path.unlink(missing_ok=True)
            elif not path.is_file() or path.read_bytes() != raw:
                _atomic_write(path, raw.decode("utf-8"))
                path.chmod(mode)
        raise


def _check_legacy_connector(home: Path) -> None:
    """Refuse a second installation identity without editing the old one."""

    old_skill = home / "skills/productivity/proactive-memory"
    targets = (
        home / "plugins/proactive-memory-tui",
        old_skill / "connectors/hermes/classic/hermes_hook.py",
        old_skill / "scripts/hermes_hook.py",
    )
    found = any(path.exists() or path.is_symlink() for path in targets)
    config = home / "config.yaml"
    if config.is_symlink():
        raise LifecycleError("Refusing symlink configuration; no files changed.")
    if config.is_file():
        # Inspect only for ownership; never print configuration or credentials.
        try:
            text = config.read_text(encoding="utf-8").replace("\\", "/")
        except (OSError, UnicodeError) as exc:
            raise LifecycleError("Cannot inspect Connector ownership in config.yaml; no files changed.") from exc
        found = found or "proactive-memory-tui" in text or "/proactive-memory/" in text
    if found:
        raise LifecycleError(
            "Legacy proactive-memory Connector detected. Use the old runtime's "
            "uninstall command first, then review any remaining old hook/plugin "
            "config entries before running sn-proactive-agent setup. No files changed."
        )


def setup_harness(
    harness: str,
    *,
    hermes_home: str | Path | None = None,
    hermes_executable: str | None = None,
    install_tui_plugin: bool = True,
    dry_run: bool = False,
    output: TextIO | None = None,
    hermes_root: str | Path | None = None,
    install_resume_bridge: bool = False,
) -> SetupResult:
    """Install the bundled Connector for one supported Harness.

    The Skill itself is intentionally not copied here.  Harness Skill managers
    own Skill installation; this operation only installs executable Connector
    resources and the narrowly scoped Hermes hook entries.
    """

    _require_harness(harness)
    out = output or sys.stdout
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser().resolve()
    _check_legacy_connector(home)
    if install_resume_bridge:
        if not install_tui_plugin or hermes_root is None:
            raise LifecycleError("完整接入需指定 --hermes-root；仅复制观测资源请显式使用 --observer-only")
        try:
            with _observer_transaction(home):
                # Preflight Hook/config conflicts before touching Hermes sources.
                setup_harness(harness, hermes_home=home, hermes_executable=hermes_executable,
                              dry_run=True, output=out)
                with install_bridge(Path(hermes_root), home, dry_run=dry_run) as base:
                    print(f"Web-only bridge: {base}（版本检查、备份、构建；卸载时恢复）", file=out)
                    if dry_run:
                        result = setup_harness(harness, hermes_home=home, hermes_executable=hermes_executable,
                                               dry_run=True, output=out)
                    else:
                        result = setup_harness(harness, hermes_home=home, hermes_executable=hermes_executable, output=out)
                        if result.warnings:
                            raise LifecycleError("观测插件未成功启用；桥接已回滚，请检查现有配置后重试")
                        _atomic_write(home / "plugins/sn-proactive-agent-tui/bridge-install.json", json.dumps({
                            "hermes_root": str(base.parent), "instance_id": instance_id(home),
                        }))
            return replace(result, bridge_root=base.parent)
        except (BridgeInstallError, OSError, subprocess.TimeoutExpired) as exc:
            raise LifecycleError(str(exc)) from exc
    try:
        with materialize_hermes_resources(
            include_tui=install_tui_plugin
        ) as resources:
            return _setup_harness_from_resources(
                home,
                resources,
                install_tui_plugin=install_tui_plugin,
                dry_run=dry_run,
                output=out,
                hermes_executable=hermes_executable,
            )
    except (FileNotFoundError, ModuleNotFoundError) as exc:
        raise LifecycleError(f"安装包中缺少 Hermes Connector 资源：{exc}") from exc
    except OSError as exc:
        raise LifecycleError(f"Hermes Connector 文件操作失败：{exc}") from exc


def _setup_harness_from_resources(
    home: Path,
    resources: object,
    *,
    install_tui_plugin: bool,
    dry_run: bool,
    output: TextIO,
    hermes_executable: str | None,
) -> SetupResult:
    """Perform setup while ``importlib.resources`` paths are materialised."""

    # Keep this small adapter local so the public lifecycle API remains stable
    # if the connector resource representation changes.
    hook_source = getattr(resources, "classic_hook", None)
    plugin_module = getattr(resources, "tui_module", None)
    plugin_source = getattr(resources, "tui_plugin", None)
    plugin_patch = getattr(resources, "tui_patch", None)
    if not isinstance(hook_source, Path) or not hook_source.is_file():
        raise LifecycleError("安装包中缺少 Hermes classic Connector")
    if install_tui_plugin:
        if not isinstance(plugin_module, Path) or not plugin_module.is_file():
            raise LifecycleError("安装包中缺少 Hermes TUI Connector 模块")
        if not isinstance(plugin_source, Path) or not plugin_source.is_file():
            raise LifecycleError("安装包中缺少 Hermes TUI 插件描述")
        if not isinstance(plugin_patch, Path) or not plugin_patch.is_file():
            raise LifecycleError("安装包中缺少 Hermes ACP/TUI 补丁")

    out = output
    config_path = home / "config.yaml"

    hook_destination = (
        home
        / "skills"
        / "productivity"
        / "sn-proactive-agent"
        / "connectors"
        / "hermes"
        / "classic"
        / "hermes_hook.py"
    )
    plugin_destination = home / "plugins" / "sn-proactive-agent-tui"
    command = str(hook_destination)
    legacy_command = str(
        home
        / "skills"
        / "productivity"
        / "sn-proactive-agent"
        / "scripts"
        / "hermes_hook.py"
    )

    existing_config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    updated_config, config_changed, migrated_legacy = _plan_hook_config(
        existing_config,
        command=command,
        legacy_command=legacy_command,
        config_exists=config_path.is_file(),
    )

    if dry_run:
        print(f"Harness: Hermes", file=out)
        print(f"Hook: {hook_source} -> {hook_destination}", file=out)
        if install_tui_plugin:
            print(f"TUI plugin: {plugin_source} -> {plugin_destination}", file=out)
        print(f"Config: {config_path}", file=out)
        if migrated_legacy:
            print(f"Config migration: {legacy_command} -> {command}", file=out)
        elif config_changed:
            print("Config: 将添加 Proactive Agent Hook", file=out)
        else:
            print("Config: 已包含 Proactive Agent Hook，无需修改", file=out)
        return SetupResult(
            "hermes",
            home,
            hook_destination,
            plugin_destination if install_tui_plugin else None,
            config_path,
            config_changed,
            (),
        )

    hook_destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(hook_source, hook_destination)
    hook_destination.chmod(0o755)

    if install_tui_plugin:
        plugin_destination.parent.mkdir(parents=True, exist_ok=True)
        _copy_tui_resources(
            plugin_destination,
            plugin_module,
            plugin_source,
            plugin_patch,
        )

    if config_changed:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        if config_path.exists():
            backup = _backup_path(config_path)
            shutil.copy2(config_path, backup)
            print(f"Hermes config backup: {backup}", file=out)
        _atomic_write(config_path, updated_config)

    warnings: list[str] = []
    if install_tui_plugin:
        executable = hermes_executable or shutil.which("hermes")
        if executable:
            message = _enable_tui_plugin(executable, home)
            if message:
                warnings.append(message)
                print(message, file=out)
        else:
            warnings.append(
                "TUI 插件已复制，但找不到 hermes；请手动运行 `hermes plugins enable sn-proactive-agent-tui`."
            )
            print(warnings[-1], file=out)

    print(f"Installed Hermes Hook: {hook_destination}", file=out)
    if install_tui_plugin:
        print(f"Installed Hermes TUI plugin: {plugin_destination}", file=out)
    return SetupResult(
        "hermes",
        home,
        hook_destination,
        plugin_destination if install_tui_plugin else None,
        config_path,
        config_changed,
        tuple(warnings),
    )


def uninstall_harness(
    harness: str,
    *,
    hermes_home: str | Path | None = None,
    data_root: str | Path | None = None,
    dry_run: bool = False,
    output: TextIO | None = None,
    hermes_root: str | Path | None = None,
) -> UninstallResult:
    """Remove only this package's Harness resources; preserve user data."""

    _require_harness(harness)
    out = output or sys.stdout
    home = Path(hermes_home or os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")).expanduser().resolve()
    bridge_pointer = home / "plugins/sn-proactive-agent-tui/bridge-install.json"
    if bridge_pointer.exists() and hermes_root is None:
        raise LifecycleError("此实例安装过续跑桥；请显式指定同一 --hermes-root 以安全恢复")
    if hermes_root is not None:
        try:
            if bridge_pointer.is_file():
                pointer = json.loads(bridge_pointer.read_text(encoding="utf-8"))
                if pointer.get("hermes_root") != str(Path(hermes_root).expanduser().resolve()) or pointer.get("instance_id") != instance_id(home):
                    raise LifecycleError("Hermes root/home 与安装记录不匹配")
            uninstall_bridge(Path(hermes_root), home=home, dry_run=dry_run)
        except (BridgeInstallError, OSError, ValueError) as exc:
            raise LifecycleError(str(exc)) from exc
    config_path = home / "config.yaml"
    hook_path = (
        home
        / "skills"
        / "productivity"
        / "sn-proactive-agent"
        / "connectors"
        / "hermes"
        / "classic"
        / "hermes_hook.py"
    )
    legacy_path = (
        home
        / "skills"
        / "productivity"
        / "sn-proactive-agent"
        / "scripts"
        / "hermes_hook.py"
    )
    plugin_path = home / "plugins" / "sn-proactive-agent-tui"
    commands = (str(hook_path), str(legacy_path))
    existing_config = config_path.read_text(encoding="utf-8") if config_path.is_file() else ""
    updated_config, config_changed = _remove_hook_commands(existing_config, commands)
    targets: list[Path] = []
    for path in (hook_path, legacy_path, plugin_path):
        if path.exists():
            targets.append(path)

    if dry_run:
        print("将移除以下 Harness 资源：", file=out)
        for path in targets:
            print(f"- {path}", file=out)
        if config_changed:
            print(f"- 配置中的 Proactive Agent Hook: {config_path}", file=out)
        print(f"保留用户数据：{resolve_data_root(data_root)}", file=out)
        return UninstallResult(
            "hermes", home, tuple(targets), config_changed, resolve_data_root(data_root)
        )

    if config_changed and config_path.exists():
        backup = _backup_path(config_path)
        shutil.copy2(config_path, backup)
        print(f"Hermes config backup: {backup}", file=out)
        _atomic_write(config_path, updated_config)

    removed: list[Path] = []
    for path in targets:
        # These are exact, package-owned destinations, never a user data root.
        if path.is_dir() and not path.is_symlink():
            shutil.rmtree(path)
        else:
            path.unlink(missing_ok=True)
        removed.append(path)
        print(f"Removed: {path}", file=out)
    print(f"保留用户数据：{resolve_data_root(data_root)}", file=out)
    return UninstallResult(
        "hermes", home, tuple(removed), config_changed, resolve_data_root(data_root)
    )


def _require_harness(harness: str) -> None:
    if not isinstance(harness, str) or harness.strip().lower() != "hermes":
        raise LifecycleError(
            f"暂不支持 Harness {harness!r}；当前只提供 Hermes 接入资源。"
        )


def _copy_tui_resources(
    destination: Path,
    module: object,
    plugin: object,
    patch: object,
) -> None:
    """Copy the wheel's TUI resources into Hermes' plugin directory."""

    if not all(isinstance(value, Path) for value in (module, plugin, patch)):
        raise LifecycleError("Hermes TUI Connector 资源路径无效")
    destination.mkdir(parents=True, exist_ok=True)
    (destination / "patches").mkdir(parents=True, exist_ok=True)
    shutil.copy2(module, destination / "__init__.py")
    shutil.copy2(plugin, destination / "plugin.yaml")
    shutil.copy2(
        patch,
        destination / "patches" / "hermes-tui-acp-submit.patch",
    )


def _inspect_hermes_resources() -> tuple[bool, str, bool, str]:
    """Check packaged resources independently without materializing files."""

    try:
        hook = connector_resource("hermes/classic/hermes_hook.py")
        hook_ok, hook_detail = True, f"包内资源：{hook}（不代表已安装到 Hermes）"
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        hook_ok, hook_detail = False, "未找到打包的 Hermes Hook"
    try:
        connector_resource("hermes/tui/__init__.py")
        plugin = connector_resource("hermes/tui/plugin.yaml")
        connector_resource("hermes/tui/patches/hermes-tui-acp-submit.patch")
        plugin_ok, plugin_detail = True, f"包内资源：{plugin}（不代表已安装或启用）"
    except (FileNotFoundError, ModuleNotFoundError, OSError):
        plugin_ok, plugin_detail = False, "打包的 Hermes 观测组件资源不完整"
    return hook_ok, hook_detail, plugin_ok, plugin_detail


def _package_root() -> Path:
    return Path(__file__).resolve().parent


def _plan_hook_config(
    existing: str,
    *,
    command: str,
    legacy_command: str,
    config_exists: bool,
) -> tuple[str, bool, bool]:
    """Return a safe config update without writing it."""

    if _config_contains_command(existing, command):
        return existing, False, False
    if _config_contains_command(existing, legacy_command):
        return _replace_config_command(existing, legacy_command, command), True, True
    if config_exists and re_search_hooks(existing):
        raise LifecycleError(
            "Hermes config 已有 hooks 配置；为避免覆盖其他 Hook，请先人工合并，"
            "或在合并后重新运行 setup。"
        )
    snippet = "\n".join(
        (
            "hooks:",
            "  pre_llm_call:",
            f"    - command: {json.dumps(command, ensure_ascii=False)}",
            "      timeout: 3",
            "  post_llm_call:",
            f"    - command: {json.dumps(command, ensure_ascii=False)}",
            "      timeout: 3",
        )
    )
    if not existing.strip():
        return snippet + "\n", True, False
    return existing.rstrip() + "\n\n" + snippet + "\n", True, False


def re_search_hooks(text: str) -> bool:
    """A tiny YAML-free check for a top-level ``hooks:`` key."""

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("#"):
            continue
        if line and not line[0].isspace() and stripped.startswith("hooks:"):
            return True
    return False


def _remove_hook_commands(existing: str, commands: Iterable[str]) -> tuple[str, bool]:
    command_set = tuple(commands)
    if not existing:
        return existing, False
    lines = existing.splitlines(keepends=True)
    kept: list[str] = []
    skip_timeout = False
    changed = False
    for line in lines:
        if any(_line_has_config_command(line, command) for command in command_set):
            changed = True
            skip_timeout = True
            continue
        if skip_timeout and line.strip().startswith("timeout:"):
            changed = True
            skip_timeout = False
            continue
        skip_timeout = False
        kept.append(line)
    cleaned, removed_empty_keys = _remove_empty_hook_keys(kept)
    return "".join(cleaned), changed or removed_empty_keys


def _remove_empty_hook_keys(lines: list[str]) -> tuple[list[str], bool]:
    """Drop Proactive Agent hook keys left empty after command removal.

    Hermes accepts a YAML mapping with unrelated hooks.  Removing only an
    empty ``pre_llm_call``/``post_llm_call`` key keeps that mapping valid and
    avoids leaving ``null`` values that some Harness versions cannot iterate.
    """

    target_keys = {"pre_llm_call:", "post_llm_call:"}
    result: list[str] = []
    changed = False
    for index, line in enumerate(lines):
        stripped = line.strip()
        if stripped not in target_keys:
            result.append(line)
            continue
        indent = len(line) - len(line.lstrip())
        next_index = index + 1
        while next_index < len(lines) and not lines[next_index].strip():
            next_index += 1
        if next_index >= len(lines):
            changed = True
            continue
        next_line = lines[next_index]
        next_indent = len(next_line) - len(next_line.lstrip())
        if next_indent <= indent:
            changed = True
            continue
        result.append(line)

    # If the top-level ``hooks:`` key is now empty, remove it too.  Comments
    # are retained; they may explain a user's other configuration.
    final: list[str] = []
    for index, line in enumerate(result):
        if line.strip() != "hooks:" or line[:1].isspace():
            final.append(line)
            continue
        next_index = index + 1
        while next_index < len(result) and not result[next_index].strip():
            next_index += 1
        if next_index >= len(result):
            changed = True
            continue
        next_line = result[next_index]
        if next_line[:1].isspace() and not next_line.lstrip().startswith("#"):
            final.append(line)
        else:
            changed = True
    return final, changed


def _config_contains_command(text: str, command: str) -> bool:
    return any(_line_has_config_command(line, command) for line in text.splitlines())


def _replace_config_command(text: str, old: str, new: str) -> str:
    lines: list[str] = []
    for line in text.splitlines(keepends=True):
        if _line_has_config_command(line, old):
            # Preserve indentation and the YAML quoting style where possible;
            # JSON quoting is valid YAML and handles spaces safely.
            prefix, _separator, _value = line.partition("command:")
            if line.endswith("\r\n"):
                newline = "\r\n"
            elif line.endswith("\n"):
                newline = "\n"
            else:
                newline = ""
            lines.append(
                f"{prefix}command: {json.dumps(new, ensure_ascii=False)}{newline}"
            )
        else:
            lines.append(line)
    return "".join(lines)


def _line_has_config_command(line: str, command: str) -> bool:
    """Match a YAML ``command`` scalar exactly, not by path substring."""

    if "command:" not in line:
        return False
    _prefix, _separator, raw = line.partition("command:")
    value = raw.strip()
    if not value:
        return False
    if value[0:1] in {"\"", "'"} and value[-1:] == value[0]:
        value = value[1:-1]
    return value == command


def _directory_writable_or_creatable(path: Path) -> bool:
    if path.exists():
        return path.is_dir() and os.access(path, os.W_OK)
    parent = path.parent
    while not parent.exists() and parent != parent.parent:
        parent = parent.parent
    return parent.is_dir() and os.access(parent, os.W_OK)


def _port_check(port: int) -> DoctorCheck:
    import socket

    if not isinstance(port, int) or not 1 <= port <= 65535:
        return DoctorCheck("port", False, f"端口无效: {port}")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", port))
    except OSError as exc:
        return DoctorCheck("port", False, f"127.0.0.1:{port} 不可用: {exc}")
    finally:
        sock.close()
    return DoctorCheck("port", True, f"127.0.0.1:{port} 可用")


def _service_check(url: str) -> DoctorCheck:
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=2) as response:
            payload = response.read(4096).decode("utf-8", errors="replace")
            status = getattr(response, "status", None)
            if status is None and hasattr(response, "getcode"):
                status = response.getcode()
        status = 200 if status is None else status
        if status != 200:
            return DoctorCheck("service", False, f"HTTP {status}: {url}")
        try:
            health = json.loads(payload)
        except ValueError:
            health = None
        if not isinstance(health, dict) or health.get("status") != "ok":
            return DoctorCheck("service", False, f"健康检查响应不符合预期: {url}")
        return DoctorCheck(
            "service", True, f"健康检查通过: {url}（不代表模型、对话采集或续跑已通过）"
        )
    except (OSError, urllib.error.URLError, TimeoutError) as exc:
        return DoctorCheck("service", False, f"无法访问 {url}: {exc}")


def _backup_path(path: Path) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    candidate = path.with_name(f"{path.name}.sn-proactive-agent.{timestamp}.bak")
    suffix = 1
    while candidate.exists():
        candidate = path.with_name(
            f"{path.name}.sn-proactive-agent.{timestamp}.{suffix}.bak"
        )
        suffix += 1
    return candidate


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


def _enable_tui_plugin(executable: str, hermes_home: Path) -> str | None:
    environment = os.environ.copy()
    environment["HERMES_HOME"] = str(hermes_home)
    try:
        result = subprocess.run(
            [executable, "plugins", "enable", "sn-proactive-agent-tui"],
            env=environment,
            capture_output=True,
            text=True,
            check=False,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        return "TUI 插件启用超时；未确认启用成功。"
    except OSError as exc:
        return f"TUI 插件已安装但无法自动启用：{exc}。请手动运行 `hermes plugins enable sn-proactive-agent-tui`."
    if result.returncode == 0:
        return None
    # A configuration parse error may echo a credential-bearing YAML line.
    # Report only the exit status, never arbitrary Harness output.
    return (
        f"TUI 插件无法自动启用（退出码 {result.returncode}）；请检查 Hermes 插件配置。"
    )


__all__ = [
    "DoctorCheck",
    "DoctorReport",
    "LifecycleError",
    "SetupResult",
    "UninstallResult",
    "default_data_root",
    "resolve_data_root",
    "run_doctor",
    "setup_harness",
    "uninstall_harness",
]
