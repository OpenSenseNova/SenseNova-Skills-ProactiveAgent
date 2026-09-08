"""Run the service or respond to a CLI suggestion."""

from __future__ import annotations

import argparse
import json
import os
import shutil
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from socketserver import ThreadingMixIn
from wsgiref.simple_server import WSGIServer, make_server

from . import __version__
from .api import create_app
from .bridge import BridgeHub
from .connector import ConnectorRouter, HermesCliConnector, HermesTuiConnector
from .contracts import SuggestionChoice, SuggestionResponded
from .core import ProactiveMemoryCore
from .daily_report import DailyReportService
from .journal import RuntimeJournal
from .lifecycle import (
    LifecycleError,
    run_doctor,
    setup_harness,
    uninstall_harness,
)
from .semantic import HermesJsonReasoner, SemanticError, SemanticJudge, SemanticOrganizer
from .storage import MarkdownStore


class _ThreadingWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _UnavailableReasoner:
    """Keep the Web dashboard available when no Harness is configured.

    A Web-only process can still serve persisted Project/Item state and accept
    health/dashboard requests without a local Hermes binary.  If an inbound
    Turn arrives before a semantic worker is configured, Core records a
    processing failure rather than pretending it can classify the Turn.
    """

    def ask_json(self, prompt: str) -> dict[str, object]:
        del prompt
        raise SemanticError(
            "未配置 Hermes 语义工作器；Web-only 模式只能展示已有状态，"
            "请配置 Harness 后再接收新对话。"
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="proactive-memory-service")
    parser.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {__version__}",
    )
    subcommands = parser.add_subparsers(dest="command")

    serve = subcommands.add_parser("serve", help="启动核心服务和 Hermes Connector")
    _add_serve_arguments(serve)
    start = subcommands.add_parser("start", help="启动核心服务（serve 的别名）")
    _add_serve_arguments(start)

    setup = subcommands.add_parser(
        "setup",
        help="安装 Hermes Connector；完整接入需指定 --hermes-root",
    )
    setup.add_argument("--harness", choices=["hermes"], default="hermes")
    setup.add_argument("--hermes-root", type=Path, help="明确允许安装 Web-only 桥接的 Hermes 源码目录")
    setup.add_argument("--observer-only", action="store_true", help="仅安装观测资源，不宣称完整接入")
    setup.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")),
        help="Hermes 配置目录（默认：~/.hermes）",
    )
    setup.add_argument(
        "--hermes-executable",
        default=os.environ.get("PROACTIVE_MEMORY_HERMES") or shutil.which("hermes"),
        help="用于启用 Hermes TUI 插件的可执行文件",
    )
    setup.add_argument(
        "--no-tui-plugin",
        action="store_true",
        help="只安装 classic CLI Hook，不安装/启用 TUI 插件",
    )
    setup.add_argument(
        "--web-only",
        action="store_true",
        help=(
            "记录 Web-only 启动口径；仍安装 TUI 观测插件，"
            "请启动服务时使用 serve --web-only"
        ),
    )
    setup.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将要执行的操作，不写文件",
    )

    doctor = subcommands.add_parser(
        "doctor",
        help="只读检查运行包、Connector、数据目录和本地服务",
    )
    doctor.add_argument("--harness", choices=["hermes"])
    doctor.add_argument("--hermes-root", type=Path, help="检查已构建的桥接安装记录")
    doctor.add_argument("--session-id", help="配合 --url 读取指定在线 Session 的真实能力证据")
    doctor.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")),
        help="检查此 Hermes 实例的观测资源，不读取配置或凭据",
    )
    doctor.add_argument(
        "--hermes-executable",
        default=os.environ.get("PROACTIVE_MEMORY_HERMES") or shutil.which("hermes"),
    )
    doctor.add_argument("--data-root", type=Path)
    doctor.add_argument(
        "--url",
        dest="service_url",
        help="检查指定服务地址（例如 http://127.0.0.1:8080）",
    )
    doctor.add_argument(
        "--check-service",
        action="store_true",
        help="主动请求本地服务 /health",
    )
    doctor.add_argument(
        "--port",
        type=int,
        dest="check_port",
        help="只读检查端口是否可绑定",
    )
    doctor.add_argument(
        "--json",
        action="store_true",
        dest="json_output",
        help="以 JSON 输出诊断结果",
    )

    uninstall = subcommands.add_parser(
        "uninstall",
        help="移除 Harness Connector（保留 ~/.proactive-memory 用户数据）",
    )
    uninstall.add_argument("--harness", choices=["hermes"], default="hermes")
    uninstall.add_argument("--hermes-root", type=Path, help="恢复此 Hermes 源码目录中的已安装桥接")
    uninstall.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME") or (Path.home() / ".hermes")),
    )
    uninstall.add_argument("--data-root", type=Path)
    uninstall.add_argument(
        "--dry-run",
        action="store_true",
        help="只显示将要移除的资源，不删除文件",
    )

    _add_respond_parser(subcommands)

    return parser


def _add_serve_arguments(serve: argparse.ArgumentParser) -> None:
    serve.add_argument(
        "--host",
        default=os.environ.get("PROACTIVE_MEMORY_HOST", "127.0.0.1"),
    )
    serve.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("PROACTIVE_MEMORY_PORT", "8080")),
    )
    serve.add_argument(
        "--data-root",
        type=Path,
        default=Path(
            os.environ.get("PROACTIVE_MEMORY_DATA_ROOT")
            or (Path.home() / ".proactive-memory")
        ),
        help=(
            "状态数据根目录（默认：~/.proactive-memory；也可用 "
            "PROACTIVE_MEMORY_DATA_ROOT 覆盖）"
        ),
    )
    serve.add_argument(
        "--hermes",
        default=os.environ.get("PROACTIVE_MEMORY_HERMES") or shutil.which("hermes"),
    )
    serve.add_argument(
        "--hermes-provider",
        default=os.environ.get("PROACTIVE_MEMORY_HERMES_PROVIDER"),
    )
    serve.add_argument(
        "--hermes-model",
        default=os.environ.get("PROACTIVE_MEMORY_HERMES_MODEL"),
    )
    serve.add_argument(
        "--web-only",
        action="store_true",
        default=False,
        help=(
            "只在 Web Dashboard 展示 suggestion.ready；保留 Hermes TUI 的 "
            "同 Session 续跑桥接"
        ),
    )


def _add_respond_parser(subcommands: argparse._SubParsersAction) -> None:
    respond = subcommands.add_parser("respond", help="同意或忽略一条 CLI 建议")
    respond.add_argument("suggestion_id")
    respond.add_argument("choice", choices=[choice.value for choice in SuggestionChoice])
    respond.add_argument(
        "--url",
        default=os.environ.get(
            "PROACTIVE_MEMORY_SERVICE_URL", "http://127.0.0.1:8080"
        ),
    )


def main(argv: list[str] | None = None) -> None:
    args = _parser().parse_args(argv)
    if args.command == "respond":
        _respond(args.suggestion_id, SuggestionChoice(args.choice), args.url)
        return
    if args.command == "doctor":
        exit_code = _doctor(args)
        if exit_code:
            raise SystemExit(exit_code)
        return
    if args.command == "setup":
        _setup(args)
        return
    if args.command == "uninstall":
        _uninstall(args)
        return
    if args.command is None:
        args = _parser().parse_args(["serve"])
    _serve(args)


def _doctor(args: argparse.Namespace) -> int:
    report = run_doctor(
        harness=args.harness,
        hermes_executable=args.hermes_executable,
        hermes_home=args.hermes_home,
        hermes_root=args.hermes_root,
        session_id=args.session_id,
        data_root=args.data_root,
        service_url=args.service_url,
        check_service=args.check_service,
        check_port=args.check_port,
    )
    if args.json_output:
        print(json.dumps(report.to_dict(), ensure_ascii=False, indent=2))
    else:
        for check in report.checks:
            marker = "?" if check.status == "unverified" else (
                "✓" if check.ok else ("!" if not check.required else "✗")
            )
            suffix = "（可选）" if not check.required else ""
            print(f"{marker} {check.name}: {check.detail}{suffix}")
        if report.ok:
            if report.scope == "hermes":
                print("Doctor: 指定在线 Session 的采集与获批回流检查通过；业务结果仍需人工验收")
            else:
                print("Doctor: 基础检查通过（不代表模型与完整接入验收通过）")
        else:
            print("Doctor: 存在失败或未验证的必需项；不能认定完整接入成功")
    return 0 if report.ok else 1


def _setup(args: argparse.Namespace) -> None:
    try:
        result = setup_harness(
            args.harness,
            hermes_home=args.hermes_home,
            hermes_executable=args.hermes_executable,
            # Web-only means that the service suppresses the native suggestion
            # renderer.  The TUI observer must still be installed so turns are
            # captured and an approved action can return to the same Session.
            install_tui_plugin=not args.no_tui_plugin,
            hermes_root=args.hermes_root,
            install_resume_bridge=not (args.no_tui_plugin or args.observer_only),
            dry_run=args.dry_run,
        )
    except LifecycleError as exc:
        raise SystemExit(f"setup 失败：{exc}") from exc
    if args.dry_run:
        return
    if args.no_tui_plugin:
        mode = "仅 classic Hook"
    elif result.bridge_root is not None:
        mode = "Web-only Connector 已安装；需重启 Hermes 并完成在线能力验收"
    else:
        mode = "仅观测资源（不包含原 Session 续跑桥）"
    print(f"Setup 完成：{mode}")


def _uninstall(args: argparse.Namespace) -> None:
    try:
        uninstall_harness(
            args.harness,
            hermes_home=args.hermes_home,
            data_root=args.data_root,
            hermes_root=args.hermes_root,
            dry_run=args.dry_run,
        )
    except LifecycleError as exc:
        raise SystemExit(f"uninstall 失败：{exc}") from exc


def _serve(args: argparse.Namespace) -> None:
    if not args.hermes and not args.web_only:
        raise SystemExit("找不到 Hermes；请用 --hermes 指定可执行文件。")
    data_root = args.data_root.expanduser().resolve()
    store = MarkdownStore(data_root)
    journal = RuntimeJournal(data_root)
    daily_reports = DailyReportService(store, journal)
    reasoner = (
        HermesJsonReasoner(
            args.hermes,
            cwd=data_root,
            provider=args.hermes_provider,
            model=args.hermes_model,
        )
        if args.hermes
        else _UnavailableReasoner()
    )
    organizer = SemanticOrganizer(reasoner)
    judge = SemanticJudge(reasoner)
    core_box: dict[str, ProactiveMemoryCore] = {}
    service_url = f"http://{args.host}:{args.port}"
    connector = (
        HermesCliConnector(
            args.hermes,
            service_url=service_url,
            failure_sink=lambda event: core_box["core"].handle(event),
            provider=args.hermes_provider,
            model=args.hermes_model,
        )
        if args.hermes
        else None
    )
    bridge = BridgeHub()
    tui_connector = HermesTuiConnector(
        bridge,
        log=print,
        show_suggestions=not args.web_only,
    )
    core = ProactiveMemoryCore(
        store,
        journal,
        organizer,
        judge,
        ConnectorRouter(
            tuple(item for item in (connector, tui_connector) if item is not None)
        ),
    )
    core_box["core"] = core
    app = create_app(
        core,
        bridge=bridge,
        store=store,
        journal=journal,
        daily_reports=daily_reports,
    )
    # Generate the previous day's closeout immediately when the service starts
    # (including after downtime), then keep a tiny midnight checker alive for
    # long-running local processes.  API requests perform the same idempotent
    # check, so the service remains correct if the scheduler is unavailable.
    try:
        daily_reports.start()
        with make_server(
            args.host,
            args.port,
            app,
            server_class=_ThreadingWSGIServer,
        ) as server:
            print(f"Proactive Memory Service listening on {service_url}", flush=True)
            print(f"Data: {data_root}", flush=True)
            print(
                f"Hermes: {args.hermes or '未配置（Web-only 展示模式）'}",
                flush=True,
            )
            print(f"Web Dashboard: {service_url}/", flush=True)
            print(
                "Hermes TUI Bridge: enabled "
                f"(native suggestions {'disabled' if args.web_only else 'enabled'})",
                flush=True,
            )
            if args.hermes_provider or args.hermes_model:
                print(
                    f"Hermes semantic/resume override: "
                    f"{args.hermes_provider or 'default'} / {args.hermes_model or 'default'}",
                    flush=True,
                )
            server.serve_forever()
    except KeyboardInterrupt:
        print("\nProactive Memory Service stopping …", flush=True)
    finally:
        core.wait_until_idle()
        core.close()
        daily_reports.close()


def _respond(suggestion_id: str, choice: SuggestionChoice, url: str) -> None:
    event = SuggestionResponded(
        suggestion_id=suggestion_id,
        choice=choice,
        responded_at=datetime.now(timezone.utc),
    )
    body = json.dumps(event.to_payload(), ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{url.rstrip('/')}/v1/events/suggestion.responded",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=3) as response:
            result = json.loads(response.read().decode("utf-8"))
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise SystemExit(f"提交建议响应失败：{exc}") from exc
    print(
        f"已提交 {choice.value}：{suggestion_id} "
        f"({result.get('event_type', 'unknown')})"
    )


if __name__ == "__main__":
    main()
