#!/usr/bin/env python3
"""Run one reproducible Proactive Memory demo through real Hermes ACP + TUI."""

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import sys
import threading
import time
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Any
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server


ROOT = Path(__file__).resolve().parents[1]
SERVICE_ROOT = ROOT / "src"
CONNECTORS_ROOT = ROOT / "src" / "proactive_memory_connectors"
for source_root in (SERVICE_ROOT, CONNECTORS_ROOT):
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))

from acp import (  # noqa: E402
    AcpClient,
    AcpConnector,
    AcpTuiPromptApplication,
    HermesAcpTarget,
    V1HttpEventSink,
)
from proactive_memory_service.api import create_app  # noqa: E402
from proactive_memory_service.bridge import BridgeHub  # noqa: E402
from proactive_memory_service.connector import (  # noqa: E402
    ConnectorRouter,
    HermesTuiSuggestionBridge,
)
from proactive_memory_service.contracts import TurnCompleted  # noqa: E402
from proactive_memory_service.core import (  # noqa: E402
    OrganizerContext,
    ProactiveMemoryCore,
)
from proactive_memory_service.journal import RuntimeJournal  # noqa: E402
from proactive_memory_service.semantic import (  # noqa: E402
    JudgeResult,
    OrganizationPlan,
)
from proactive_memory_service.storage import (  # noqa: E402
    ItemState,
    ItemUpdate,
    MarkdownStore,
    OrganizedTurn,
    ProjectMetadata,
)


DEMO_CHOICES = ("demo-01", "demo-02", "demo-03")
DEMO_ID = "demo-02"
PROJECT_ID = "project-001"
ITEM_ID = "item-001"

DEMO01_FIRST_PROMPT = (
    "我计划在 2026 年 10 月 31 日前发布公司内部 Python SDK v1.0。"
    "公开 API 只包含 Client、create_task 和 get_task_status。"
    "目前 API 范围已经确定，核心实现、安装文档和构建验证尚未完成。"
    "请只帮我整理当前目标和进展，不要写代码。"
)
DEMO01_SECOND_PROMPT = (
    "继续推进之前的公司内部 Python SDK v1.0。核心实现、安装文档、"
    "构建验证、版本号、CHANGELOG 和回滚方案都已经完成。"
    "目前只等运维在明天下午确认发布窗口；确认前不发布，"
    "也没有其他可执行事项。请只总结当前进展。"
)

DEMO03_FIRST_PROMPT = (
    "请持续跟踪“上线客户反馈表单”。完成标准有三条：公开链接可以访问、"
    "必填字段可以成功提交、提交后显示确认提示。"
    "请只帮我检查这三条标准是否清楚。"
)
DEMO03_SECOND_PROMPT = (
    "客户反馈表单的公开链接已经可以访问，必填字段提交成功，"
    "提交后也会显示确认提示。我已经逐项验证，"
    "请把“上线客户反馈表单”标记为完成。"
)
DEMO03_THIRD_PROMPT = (
    "刚发现一个反例：手机端打开客户反馈表单时，提交按钮会被底部导航遮住，"
    "用户实际无法完成提交。所以这件事还不能算完成。"
    "请先记录问题和影响，不要现在写修复代码。"
)
DEMO03_SUGGESTED_ACTION = (
    "请制定客户反馈表单的移动端修复与验证清单；"
    "只使用已确认的问题，不写代码。"
)

BLOCKED_PROMPT = (
    "星河科技新品发布页的页面结构和交互原型已经确认，合同要求 "
    "2026 年 9 月 18 日前交付正式页面。但客户尚未提供最终版 Logo、"
    "品牌色规范、产品主视觉和正式宣传文案，四项材料缺少任意一项都不能开工，"
    "因此前端制作目前必须保持暂停，前端开工任务单也尚未制定。"
    "请帮我整理当前项目状态，不要调用工具。"
)
READY_PROMPT = (
    "客户刚刚确认星河科技新品发布页的最终版 Logo、品牌色规范、"
    "产品主视觉和正式宣传文案已经全部交付，四项材料均验收无误。"
    "我要统一归档，请按“星河科技_素材类型_最终版_20260828”的格式列出四个新文件名。"
    "只直接回答文件名，不调用工具，也不要制定前端开工计划。"
)
SUGGESTED_ACTION = (
    "请在当前 Session 生成一份前端开工任务单和排期草案；"
    "只使用已确认事实，未知页面和负责人标记为待确认，不调用工具。"
)


class QuietWsgiHandler(WSGIRequestHandler):
    def log_message(self, _format: str, *args: object) -> None:
        del args


class ThreadingWsgiServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class Demo02Organizer:
    """Deterministic Case 6 state transitions around real ACP answers."""

    project = ProjectMetadata(
        id=PROJECT_ID,
        name="星河科技新品发布页",
        summary="完成新品发布页交付并推进前端制作。",
    )
    initial_item = ItemState(
        id=ITEM_ID,
        name="前端制作",
        status="blocked",
        goal="在客户材料齐备后完成新品发布页前端制作。",
        completion_criteria="正式页面完成并满足交付要求。",
        current_progress="页面结构和交互原型已确认；等待四项客户材料。",
        next_step="材料齐备后制定前端开工任务单并排期。",
        blocker="最终版 Logo、品牌色规范、产品主视觉和正式宣传文案尚未提供。",
    )

    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> OrganizationPlan:
        del context
        if turn.source_suggestion_id is not None:
            summary = "已在原 ACP Session 生成前端开工任务单和排期草案。"
            updates = ItemUpdate(
                {
                    "status": "in_progress",
                    "current_progress": (
                        "前端开工任务单和排期草案已生成，"
                        "未知页面与负责人保留为待确认。"
                    ),
                    "next_step": "确认页面范围和负责人后开始前端制作。",
                    "blocker": "页面范围和负责人仍有待确认项。",
                }
            )
            reason = "本轮是用户已授权建议在原 ACP Session 中的执行结果。"
        elif "已经全部交付" in turn.user_question:
            summary = "四项客户材料已全部交付并验收，前端制作阻塞已解除。"
            updates = ItemUpdate(
                {
                    "status": "in_progress",
                    "current_progress": (
                        "页面结构、交互原型和四项客户材料均已确认；"
                        "前端制作条件已满足。"
                    ),
                    "next_step": "制定前端开工任务单并排期。",
                    "blocker": None,
                }
            )
            reason = "新 QA 继续更新原前端制作 Item 并清除材料阻塞。"
        else:
            summary = "页面结构和交互原型已确认，但四项客户材料尚未齐备。"
            updates = ItemUpdate(
                {
                    "status": "blocked",
                    "current_progress": (
                        "页面结构和交互原型已确认；"
                        "等待四项客户材料。"
                    ),
                    "next_step": "材料齐备后制定前端开工任务单并排期。",
                    "blocker": (
                        "最终版 Logo、品牌色规范、产品主视觉和"
                        "正式宣传文案尚未提供。"
                    ),
                }
            )
            reason = "首轮 QA 建立了一个被明确前置条件阻塞的前端制作 Item。"

        return OrganizationPlan(
            project=self.project,
            initial_item=self.initial_item,
            event=OrganizedTurn(
                project_id=PROJECT_ID,
                item_id=ITEM_ID,
                summary=summary,
                updates=updates,
            ),
            reason=reason,
        )


class Demo02Judge:
    def judge(
        self,
        turn: TurnCompleted,
        _project: ProjectMetadata,
        item: ItemState,
        _event_summary: str,
    ) -> JudgeResult:
        if item.status == "blocked" or item.blocker:
            return JudgeResult(
                False,
                "前端制作仍被四项客户材料阻塞，当前没有可安全执行的开工动作。",
            )
        if "已经全部交付" not in turn.user_question:
            return JudgeResult(False, "本轮没有出现新的可执行时机。")
        return JudgeResult(
            True,
            "四项开工材料刚刚全部验收，前端制作阻塞已解除，现在适合推进开工准备。",
            title="制定前端开工任务单并排期",
            evidence="四项客户材料已全部交付验收；Item blocker 已清除；交付日期已明确。",
            suggested_action=SUGGESTED_ACTION,
        )


class Demo01Organizer:
    """Keep one SDK delivery Item coherent across two real ACP Sessions."""

    project = ProjectMetadata(
        id=PROJECT_ID,
        name="公司内部 Python SDK v1.0 发布",
        summary="跟踪内部 Python SDK v1.0 从范围确认到正式发布。",
    )
    initial_item = ItemState(
        id=ITEM_ID,
        name="发布公司内部 Python SDK v1.0",
        status="in_progress",
        goal="在 2026 年 10 月 31 日前发布公司内部 Python SDK v1.0。",
        completion_criteria=(
            "核心实现、安装文档、构建验证、版本号、CHANGELOG、"
            "回滚方案和发布窗口均确认。"
        ),
        current_progress="公开 API 范围已经确定。",
        next_step="完成核心实现、安装文档和构建验证。",
        blocker=None,
    )

    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> OrganizationPlan:
        del context
        if "继续推进之前" in turn.user_question:
            summary = "SDK 发布准备已完成，目前只等待运维确认发布窗口。"
            updates = ItemUpdate(
                {
                    "status": "blocked",
                    "current_progress": (
                        "核心实现、安装文档、构建验证、版本号、"
                        "CHANGELOG 和回滚方案均已完成。"
                    ),
                    "next_step": "等待运维在明天下午确认发布窗口。",
                    "blocker": "运维尚未确认发布窗口；确认前不发布。",
                }
            )
            reason = "新 Session 明确延续同一个 SDK 发布目标并更新最新状态。"
        else:
            summary = "SDK v1.0 的目标、截止时间和公开 API 范围已经确定。"
            updates = ItemUpdate(
                {
                    "status": "in_progress",
                    "current_progress": "公开 API 范围已经确定。",
                    "next_step": "完成核心实现、安装文档和构建验证。",
                    "blocker": None,
                }
            )
            reason = "首轮 QA 建立 SDK 发布 Project 和 Item。"

        return OrganizationPlan(
            project=self.project,
            initial_item=self.initial_item,
            event=OrganizedTurn(
                project_id=PROJECT_ID,
                item_id=ITEM_ID,
                summary=summary,
                updates=updates,
            ),
            reason=reason,
        )


class Demo01Judge:
    def judge(
        self,
        _turn: TurnCompleted,
        _project: ProjectMetadata,
        item: ItemState,
        _event_summary: str,
    ) -> JudgeResult:
        if item.status == "blocked":
            return JudgeResult(
                False,
                "唯一下一步依赖运维确认发布窗口，当前没有可安全执行的动作。",
            )
        return JudgeResult(
            False,
            "本轮只建立并整理长期目标，尚无需要主动提醒的时机。",
        )


class Demo03Organizer:
    """Reopen one completed Item when later evidence invalidates completion."""

    project = ProjectMetadata(
        id=PROJECT_ID,
        name="客户反馈表单上线",
        summary="跟踪客户反馈表单的可访问性、可提交性和确认反馈。",
    )
    initial_item = ItemState(
        id=ITEM_ID,
        name="上线客户反馈表单",
        status="in_progress",
        goal="让客户能够完整提交公开反馈表单。",
        completion_criteria=(
            "公开链接可以访问；必填字段可以成功提交；"
            "提交后显示确认提示。"
        ),
        current_progress="三条完成标准已经明确，等待逐项验证。",
        next_step="逐项验证三条完成标准。",
        blocker=None,
    )

    def organize(
        self,
        turn: TurnCompleted,
        context: OrganizerContext,
    ) -> OrganizationPlan:
        del context
        if "刚发现一个反例" in turn.user_question:
            summary = "移动端提交按钮被底部导航遮挡，原完成判断被新证据推翻。"
            updates = ItemUpdate(
                {
                    "status": "in_progress",
                    "current_progress": (
                        "桌面端三项验证曾通过；新发现手机端提交按钮被底部导航遮挡，"
                        "用户无法完成提交。"
                    ),
                    "next_step": "修复移动端按钮遮挡并重新验证完整提交流程。",
                    "blocker": "手机端提交按钮被底部导航遮挡。",
                }
            )
            reason = "跨 Session 的新反例属于原 Item，必须重新打开并覆盖旧完成状态。"
        elif "逐项验证" in turn.user_question:
            summary = "三条完成标准已经逐项验证，表单当时可标记为完成。"
            updates = ItemUpdate(
                {
                    "status": "completed",
                    "current_progress": (
                        "公开链接可访问、必填字段提交成功、"
                        "提交后确认提示均已验证。"
                    ),
                    "next_step": "无。",
                    "blocker": None,
                }
            )
            reason = "本轮逐项确认三条完成标准，更新原 Item 为完成。"
        else:
            summary = "客户反馈表单的三条完成标准已经明确。"
            updates = ItemUpdate(
                {
                    "status": "in_progress",
                    "current_progress": "三条完成标准已经明确，等待逐项验证。",
                    "next_step": "逐项验证三条完成标准。",
                    "blocker": None,
                }
            )
            reason = "首轮 QA 建立客户反馈表单 Project 和 Item。"

        return OrganizationPlan(
            project=self.project,
            initial_item=self.initial_item,
            event=OrganizedTurn(
                project_id=PROJECT_ID,
                item_id=ITEM_ID,
                summary=summary,
                updates=updates,
            ),
            reason=reason,
        )


class Demo03Judge:
    def judge(
        self,
        turn: TurnCompleted,
        _project: ProjectMetadata,
        _item: ItemState,
        _event_summary: str,
    ) -> JudgeResult:
        if "刚发现一个反例" in turn.user_question:
            return JudgeResult(
                True,
                "新发现的移动端阻塞推翻了原完成状态，现在适合补一份修复与验证清单。",
                title="制定移动端修复与验证计划",
                evidence=(
                    "手机端提交按钮被底部导航遮挡，用户无法完成提交；"
                    "Item 已从 completed 重新打开为 in_progress。"
                ),
                suggested_action=DEMO03_SUGGESTED_ACTION,
            )
        if "逐项验证" in turn.user_question:
            return JudgeResult(
                False,
                "三条完成标准已逐项验证，本轮没有需要额外推进的事项。",
            )
        return JudgeResult(
            False,
            "本轮只确认完成标准，尚未形成需要主动提醒的时机。",
        )


def write_json(path: Path, payload: dict[str, Any]) -> None:
    rendered = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    temporary.write_text(rendered, encoding="utf-8")
    temporary.replace(path)


def tui_command(service_url: str, hermes: Path, session_id: str) -> str:
    environment = [
        f"PROACTIVE_MEMORY_SERVICE_URL={shlex.quote(service_url)}",
        "PROACTIVE_MEMORY_TUI=1",
        "PROACTIVE_MEMORY_ACP_SUBMIT=1",
    ]
    hermes_home = os.environ.get("HERMES_HOME")
    if hermes_home:
        environment.append(f"HERMES_HOME={shlex.quote(hermes_home)}")
    command = [
        shlex.quote(str(hermes)),
        "--tui",
        "--accept-hooks",
        "--resume",
        shlex.quote(session_id),
    ]
    return " ".join((*environment, *command))


def run_demo_02(args: argparse.Namespace) -> int:
    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"Demo 目录必须为空：{output_dir}")
    data_root = output_dir / "data"
    agent_cwd = output_dir / "agent-work"
    agent_cwd.mkdir(parents=True)
    state_file = output_dir / "demo-state.json"
    result_file = output_dir / "demo-result.json"

    store = MarkdownStore(data_root)
    journal = RuntimeJournal(data_root)
    bridge = BridgeHub()
    bridge_logs: list[str] = []
    core_logs: list[str] = []
    tui_bridge = HermesTuiSuggestionBridge(bridge, log=bridge_logs.append)
    target = HermesAcpTarget(args.hermes)
    http_server: WSGIServer | None = None
    http_thread: threading.Thread | None = None
    core: ProactiveMemoryCore | None = None

    try:
        with target.transport(agent_cwd) as transport:
            client = AcpClient(transport, platform="hermes-acp")
            initialization = client.initialize()
            session = client.new_session(agent_cwd)
            connector = AcpConnector(
                "hermes-acp",
                client,
                cwd=agent_cwd,
                suggestion_sink=tui_bridge,
                resume_result_sink=tui_bridge.publish_resume_result,
                log=core_logs.append,
            )
            core = ProactiveMemoryCore(
                store,
                journal,
                Demo02Organizer(),
                Demo02Judge(),
                ConnectorRouter((connector,)),
                asynchronous=False,
                log=core_logs.append,
            )
            http_server = make_server(
                args.host,
                args.port,
                AcpTuiPromptApplication(
                    create_app(core, bridge=bridge, store=store, journal=journal),
                    client,
                    session_id=session.session_id,
                ),
                server_class=ThreadingWsgiServer,
                handler_class=QuietWsgiHandler,
            )
            service_url = f"http://{args.host}:{http_server.server_port}"
            http_thread = threading.Thread(
                target=http_server.serve_forever,
                name="hermes-acp-demo-http",
                daemon=True,
            )
            http_thread.start()
            client.event_sink = V1HttpEventSink(
                service_url,
                timeout_seconds=30,
                fail_open=False,
            )

            command = tui_command(service_url, args.hermes, session.session_id)
            waiting_payload = {
                "status": "waiting_for_tui_questions",
                "demo": DEMO_ID,
                "service_url": service_url,
                "session_id": session.session_id,
                "blocked_prompt": BLOCKED_PROMPT,
                "ready_prompt": READY_PROMPT,
                "suggested_action": SUGGESTED_ACTION,
                "tui_command": command,
            }
            write_json(state_file, waiting_payload)
            print(json.dumps(waiting_payload, ensure_ascii=False), flush=True)

            deadline = time.monotonic() + args.timeout
            completion_record = None
            suggestion: dict[str, Any] | None = None
            while time.monotonic() < deadline:
                _cursor, live_records = bridge.poll(
                    "hermes-tui",
                    session.session_id,
                )
                if suggestion is None:
                    suggestion_record = next(
                        (
                            record
                            for record in live_records
                            if record.event_type == "suggestion.ready"
                        ),
                        None,
                    )
                    if suggestion_record is not None:
                        suggestion = dict(suggestion_record.payload)
                        write_json(
                            state_file,
                            {
                                **waiting_payload,
                                "status": "waiting_for_tui_approval",
                                "suggestion_id": suggestion["suggestion_id"],
                            },
                        )
                completion_record = next(
                    (
                        record
                        for record in live_records
                        if record.event_type
                        in {"session.resume.completed", "session.resume.failed"}
                    ),
                    None,
                )
                if completion_record is not None:
                    break
                time.sleep(0.2)
            if completion_record is None:
                raise TimeoutError("等待 TUI 问题、建议授权和 ACP 续跑超时")
            if suggestion is None:
                raise RuntimeError("TUI 没有收到 suggestion.ready")

            connector.wait_until_idle(timeout_seconds=20)
            time.sleep(args.linger_seconds)
            records = journal.records()
            completed_turns = journal.records("turn.completed")
            source_turns = [
                record
                for record in completed_turns
                if record.payload.get("source_suggestion_id")
                == suggestion["suggestion_id"]
            ]
            response_records = journal.records("suggestion.responded")
            decisions = journal.records("judge.decision")
            _final_cursor, final_bridge_records = bridge.poll(
                "hermes-tui",
                session.session_id,
            )
            item = store.read_item(PROJECT_ID, ITEM_ID)
            events = store.read_events(PROJECT_ID, ITEM_ID)
            decision_outcomes = [record.payload.get("outcome") for record in decisions]
            initial_turns = [
                record
                for record in completed_turns
                if not record.payload.get("source_suggestion_id")
            ]
            blocked_payload = initial_turns[0].payload if len(initial_turns) >= 1 else {}
            ready_payload = initial_turns[1].payload if len(initial_turns) >= 2 else {}
            checks = {
                "real_hermes_agent_initialized": (
                    dict(initialization.agent_info or {}).get("name")
                    == "hermes-agent"
                ),
                "same_acp_session_for_all_turns": (
                    len(completed_turns) == 3
                    and {record.payload.get("session_id") for record in completed_turns}
                    == {session.session_id}
                ),
                "visible_tui_submitted_both_initial_questions": (
                    len(initial_turns) == 2
                    and blocked_payload.get("user_question") == BLOCKED_PROMPT
                    and ready_payload.get("user_question") == READY_PROMPT
                ),
                "blocked_turn_was_silent": decision_outcomes[:1] == ["silent"],
                "materials_ready_created_suggestion": (
                    len(journal.records("suggestion.ready")) == 1
                    and "suggest" in decision_outcomes
                ),
                "tui_approved_once": (
                    len(response_records) == 1
                    and response_records[0].payload.get("choice") == "approve"
                ),
                "acp_resume_completed": (
                    completion_record.event_type == "session.resume.completed"
                    and len(source_turns) == 1
                ),
                "source_result_did_not_recurse": (
                    decision_outcomes[-1:] == ["source_suggestion_completed"]
                    and len(journal.records("suggestion.ready")) == 1
                ),
                "one_project_and_item_updated": (
                    len(store.list_projects()) == 1
                    and item.id == ITEM_ID
                    and item.status == "in_progress"
                    and item.next_step
                    == "确认页面范围和负责人后开始前端制作。"
                ),
                "three_qa_events_preserved": events.count("<!-- event:start ") == 3,
                "no_processing_or_resume_failure": (
                    not journal.records("processing.failed")
                    and not journal.records("session.resume.failed")
                    and not journal.records("suggestion.delivery.failed")
                ),
            }
            passed = all(checks.values())
            result_payload = {
                "passed": passed,
                "scope": "demo_02_visible_hermes_tui_submissions_over_real_acp",
                "demo": DEMO_ID,
                "service_url": service_url,
                "session_id": session.session_id,
                "suggestion_id": suggestion["suggestion_id"],
                "agent_info": dict(initialization.agent_info or {}),
                "turns": {
                    "blocked": {
                        "question": BLOCKED_PROMPT,
                        "answer": blocked_payload.get("final_answer"),
                    },
                    "materials_ready": {
                        "question": READY_PROMPT,
                        "answer": ready_payload.get("final_answer"),
                    },
                    "authorized_resume": {
                        "question": SUGGESTED_ACTION,
                        "answer": (
                            source_turns[0].payload.get("final_answer")
                            if source_turns
                            else None
                        ),
                    },
                },
                "tui_command": command,
                "bridge_event_types": [
                    record.event_type for record in final_bridge_records
                ],
                "runtime_kinds": [record.kind for record in records],
                "decision_outcomes": decision_outcomes,
                "checks": checks,
                "bridge_logs": bridge_logs,
                "core_logs": core_logs,
                "artifacts": {
                    "project": str(data_root / "projects" / PROJECT_ID / "project.md"),
                    "item": str(
                        data_root
                        / "projects"
                        / PROJECT_ID
                        / "items"
                        / ITEM_ID
                        / "item.md"
                    ),
                    "events": str(
                        data_root
                        / "projects"
                        / PROJECT_ID
                        / "items"
                        / ITEM_ID
                        / "events.md"
                    ),
                    "runtime": str(data_root / "runtime.jsonl"),
                },
            }
            write_json(result_file, result_payload)
            write_json(
                state_file,
                {
                    **waiting_payload,
                    "status": "completed" if passed else "failed",
                    "suggestion_id": suggestion["suggestion_id"],
                    "result_file": str(result_file),
                },
            )
            print(json.dumps(result_payload, ensure_ascii=False, indent=2), flush=True)
            return 0 if passed else 1
    except Exception as exc:
        failure = {
            "passed": False,
            "status": "failed",
            "demo": DEMO_ID,
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "runtime_kinds": [record.kind for record in journal.records()],
            "core_logs": core_logs,
        }
        write_json(result_file, failure)
        write_json(state_file, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), flush=True)
        return 1
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
        if http_thread is not None:
            http_thread.join(timeout=2)
        if core is not None:
            core.close()


def run_cross_session_demo(args: argparse.Namespace) -> int:
    if args.demo == "demo-01":
        organizer = Demo01Organizer()
        judge = Demo01Judge()
        prompts = (DEMO01_FIRST_PROMPT, DEMO01_SECOND_PROMPT)
        required_turns = 2
    elif args.demo == "demo-03":
        organizer = Demo03Organizer()
        judge = Demo03Judge()
        prompts = (
            DEMO03_FIRST_PROMPT,
            DEMO03_SECOND_PROMPT,
            DEMO03_THIRD_PROMPT,
        )
        required_turns = 3
    else:
        raise ValueError(f"unsupported cross-session demo: {args.demo}")

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    if any(output_dir.iterdir()):
        raise SystemExit(f"Demo 目录必须为空：{output_dir}")
    data_root = output_dir / "data"
    agent_cwd = output_dir / "agent-work"
    agent_cwd.mkdir(parents=True)
    state_file = output_dir / "demo-state.json"
    result_file = output_dir / "demo-result.json"

    store = MarkdownStore(data_root)
    journal = RuntimeJournal(data_root)
    bridge = BridgeHub()
    bridge_logs: list[str] = []
    core_logs: list[str] = []
    tui_bridge = HermesTuiSuggestionBridge(bridge, log=bridge_logs.append)
    target = HermesAcpTarget(args.hermes)
    http_server: WSGIServer | None = None
    http_thread: threading.Thread | None = None
    core: ProactiveMemoryCore | None = None

    try:
        with target.transport(agent_cwd) as transport:
            client = AcpClient(transport, platform="hermes-acp")
            initialization = client.initialize()
            initial_session = client.new_session(agent_cwd)
            actual_session_ids = [initial_session.session_id]

            def create_additional_session() -> str:
                created = client.new_session(agent_cwd)
                actual_session_ids.append(created.session_id)
                return created.session_id

            connector = AcpConnector(
                "hermes-acp",
                client,
                cwd=agent_cwd,
                suggestion_sink=tui_bridge,
                resume_result_sink=tui_bridge.publish_resume_result,
                log=core_logs.append,
            )
            core = ProactiveMemoryCore(
                store,
                journal,
                organizer,
                judge,
                ConnectorRouter((connector,)),
                asynchronous=False,
                log=core_logs.append,
            )
            http_server = make_server(
                args.host,
                args.port,
                AcpTuiPromptApplication(
                    create_app(core, bridge=bridge, store=store, journal=journal),
                    client,
                    session_id=initial_session.session_id,
                    session_factory=create_additional_session,
                    max_sessions=2,
                ),
                server_class=ThreadingWsgiServer,
                handler_class=QuietWsgiHandler,
            )
            service_url = f"http://{args.host}:{http_server.server_port}"
            http_thread = threading.Thread(
                target=http_server.serve_forever,
                name=f"hermes-acp-{args.demo}-http",
                daemon=True,
            )
            http_thread.start()
            client.event_sink = V1HttpEventSink(
                service_url,
                timeout_seconds=30,
                fail_open=False,
            )

            command = tui_command(
                service_url,
                args.hermes,
                initial_session.session_id,
            )
            waiting_payload = {
                "status": "waiting_for_tui_questions",
                "demo": args.demo,
                "service_url": service_url,
                "session_id": initial_session.session_id,
                "prompts": list(prompts),
                "tui_command": command,
                "instructions": (
                    ["发送第 1 条消息", "输入 /new 并确认", "发送第 2 条消息"]
                    if args.demo == "demo-01"
                    else [
                        "发送第 1 条消息",
                        "发送第 2 条消息",
                        "输入 /new 并确认",
                        "发送第 3 条消息",
                        "在建议框保留忽略并按 Enter",
                    ]
                ),
            }
            write_json(state_file, waiting_payload)
            print(json.dumps(waiting_payload, ensure_ascii=False), flush=True)

            deadline = time.monotonic() + args.timeout
            suggestion: dict[str, Any] | None = None
            response_record = None
            while time.monotonic() < deadline:
                completed_turns = journal.records("turn.completed")
                suggestion_records = journal.records("suggestion.ready")
                if suggestion is None and suggestion_records:
                    suggestion = dict(suggestion_records[0].payload)
                    write_json(
                        state_file,
                        {
                            **waiting_payload,
                            "status": "waiting_for_tui_approval",
                            "suggestion_id": suggestion["suggestion_id"],
                        },
                    )
                response_records = journal.records("suggestion.responded")
                response_record = response_records[0] if response_records else None
                if args.demo == "demo-01":
                    if (
                        len(completed_turns) >= required_turns
                        and len(journal.records("judge.decision")) >= required_turns
                    ):
                        break
                elif len(completed_turns) >= required_turns and response_record is not None:
                    break
                time.sleep(0.2)
            else:
                raise TimeoutError("等待真实 Hermes TUI 跨 Session 操作超时")

            connector.wait_until_idle(timeout_seconds=20)
            time.sleep(args.linger_seconds)
            records = journal.records()
            completed_turns = journal.records("turn.completed")
            decisions = journal.records("judge.decision")
            decision_outcomes = [record.payload.get("outcome") for record in decisions]
            session_ids = [str(record.payload.get("session_id")) for record in completed_turns]
            item = store.read_item(PROJECT_ID, ITEM_ID)
            events = store.read_events(PROJECT_ID, ITEM_ID)
            bridge_event_types: list[str] = []
            for actual_session_id in actual_session_ids:
                _cursor, session_records = bridge.poll(
                    "hermes-tui",
                    actual_session_id,
                )
                bridge_event_types.extend(record.event_type for record in session_records)

            shared_checks = {
                "real_hermes_agent_initialized": (
                    dict(initialization.agent_info or {}).get("name")
                    == "hermes-agent"
                ),
                "visible_tui_submitted_all_questions": (
                    len(completed_turns) == required_turns
                    and [record.payload.get("user_question") for record in completed_turns]
                    == list(prompts)
                ),
                "two_real_acp_sessions_used": (
                    len(actual_session_ids) == 2
                    and len(set(actual_session_ids)) == 2
                    and len(set(session_ids)) == 2
                ),
                "one_project_and_item_preserved": (
                    len(store.list_projects()) == 1 and item.id == ITEM_ID
                ),
                "all_qa_events_preserved": (
                    events.count("<!-- event:start ") == required_turns
                    and all(prompt in events for prompt in prompts)
                ),
                "no_processing_or_delivery_failure": (
                    not journal.records("processing.failed")
                    and not journal.records("session.resume.failed")
                    and not journal.records("suggestion.delivery.failed")
                ),
            }
            if args.demo == "demo-01":
                scenario_checks = {
                    "first_and_second_turn_are_in_different_sessions": (
                        len(session_ids) == 2 and session_ids[0] != session_ids[1]
                    ),
                    "both_turns_were_silent": decision_outcomes == ["silent", "silent"],
                    "no_suggestion_created": (
                        not journal.records("suggestion.ready")
                        and not journal.records("suggestion.responded")
                    ),
                    "latest_sdk_state_was_recalled_and_updated": (
                        item.status == "blocked"
                        and item.current_progress
                        == (
                            "核心实现、安装文档、构建验证、版本号、"
                            "CHANGELOG 和回滚方案均已完成。"
                        )
                        and item.next_step == "等待运维在明天下午确认发布窗口。"
                        and item.blocker == "运维尚未确认发布窗口；确认前不发布。"
                    ),
                }
                scope = "demo_01_visible_cross_session_memory_over_real_acp"
            else:
                suggestion_records = journal.records("suggestion.ready")
                response_records = journal.records("suggestion.responded")
                scenario_checks = {
                    "first_two_turns_share_session_then_new_session": (
                        len(session_ids) == 3
                        and session_ids[0] == session_ids[1]
                        and session_ids[2] != session_ids[1]
                    ),
                    "completion_then_correction_decisions": (
                        decision_outcomes == ["silent", "silent", "suggest"]
                    ),
                    "one_correction_suggestion_created": len(suggestion_records) == 1,
                    "tui_ignored_once": (
                        len(response_records) == 1
                        and response_records[0].payload.get("choice") == "ignore"
                    ),
                    "ignore_did_not_resume_session": (
                        not journal.records("session.resume.requested")
                        and not journal.records("suggestion.executed")
                    ),
                    "completed_item_was_reopened_by_new_evidence": (
                        item.status == "in_progress"
                        and item.next_step
                        == "修复移动端按钮遮挡并重新验证完整提交流程。"
                        and item.blocker == "手机端提交按钮被底部导航遮挡。"
                    ),
                }
                scope = "demo_03_visible_cross_session_state_correction_over_real_acp"

            checks = {**shared_checks, **scenario_checks}
            passed = all(checks.values())
            result_payload = {
                "passed": passed,
                "scope": scope,
                "demo": args.demo,
                "service_url": service_url,
                "initial_session_id": initial_session.session_id,
                "actual_session_ids": actual_session_ids,
                "suggestion_id": None if suggestion is None else suggestion["suggestion_id"],
                "agent_info": dict(initialization.agent_info or {}),
                "turns": [
                    {
                        "session_id": record.payload.get("session_id"),
                        "question": record.payload.get("user_question"),
                        "answer": record.payload.get("final_answer"),
                    }
                    for record in completed_turns
                ],
                "tui_command": command,
                "bridge_event_types": bridge_event_types,
                "runtime_kinds": [record.kind for record in records],
                "decision_outcomes": decision_outcomes,
                "checks": checks,
                "bridge_logs": bridge_logs,
                "core_logs": core_logs,
                "artifacts": {
                    "project": str(data_root / "projects" / PROJECT_ID / "project.md"),
                    "item": str(
                        data_root
                        / "projects"
                        / PROJECT_ID
                        / "items"
                        / ITEM_ID
                        / "item.md"
                    ),
                    "events": str(
                        data_root
                        / "projects"
                        / PROJECT_ID
                        / "items"
                        / ITEM_ID
                        / "events.md"
                    ),
                    "runtime": str(data_root / "runtime.jsonl"),
                },
            }
            write_json(result_file, result_payload)
            final_state = {
                **waiting_payload,
                "status": "completed" if passed else "failed",
                "result_file": str(result_file),
            }
            if suggestion is not None:
                final_state["suggestion_id"] = suggestion["suggestion_id"]
            write_json(state_file, final_state)
            print(json.dumps(result_payload, ensure_ascii=False, indent=2), flush=True)
            return 0 if passed else 1
    except Exception as exc:
        failure = {
            "passed": False,
            "status": "failed",
            "demo": args.demo,
            "error_type": type(exc).__name__,
            "reason": str(exc),
            "runtime_kinds": [record.kind for record in journal.records()],
            "core_logs": core_logs,
        }
        write_json(result_file, failure)
        write_json(state_file, failure)
        print(json.dumps(failure, ensure_ascii=False, indent=2), flush=True)
        return 1
    finally:
        if http_server is not None:
            http_server.shutdown()
            http_server.server_close()
        if http_thread is not None:
            http_thread.join(timeout=2)
        if core is not None:
            core.close()


def run(args: argparse.Namespace) -> int:
    if args.demo == DEMO_ID:
        return run_demo_02(args)
    return run_cross_session_demo(args)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--demo", choices=DEMO_CHOICES, required=True)
    parser.add_argument(
        "--hermes",
        type=Path,
        default=Path(shutil.which("hermes") or "hermes"),
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8094)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--timeout", type=float, default=600)
    parser.add_argument("--linger-seconds", type=float, default=3)
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(run(parse_args()))
