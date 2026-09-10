#!/usr/bin/env python3
"""Deterministic semantic worker for the Case 6 Hermes TUI demonstration.

The interactive side of the demonstration still runs the real Hermes TUI.  This
worker only makes Organizer/Judge decisions predictable so the screenshots can
be reproduced without waiting for a second model to decide whether to remind.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path


PROJECT_ID = "project-001"
ITEM_ID = "item-001"


def _is_materials_blocked(prompt: str) -> bool:
    return "尚未提供" in prompt and "四项材料" in prompt


def _is_materials_ready(prompt: str) -> bool:
    return "已经全部交付" in prompt or "全部交付" in prompt


def _is_resume_action(prompt: str) -> bool:
    return "开工任务单" in prompt and (
        "待确认" in prompt or "排期" in prompt
    )


def response_for(prompt: str) -> dict[str, object]:
    if "Organizer 第一层路由器" in prompt:
        # The prompt intentionally includes the previous Item state.  Once the
        # client has delivered the four assets, both the old blocker text and
        # the new "全部交付" text are present; the new fact must win.
        if _is_materials_ready(prompt):
            return {
                "route": "existing_project",
                "project_id": PROJECT_ID,
                "reason": "QA 继续更新已有的新品发布页项目。",
            }
        if _is_materials_blocked(prompt):
            return {
                "route": "new_project",
                "project_name": "星河科技新品发布页",
                "project_summary": "完成新品发布页交付并推进前端制作。",
                "reason": "QA 明确描述了一个需要持续跟踪的交付项目。",
            }
        if _is_resume_action(prompt):
            return {
                "route": "existing_project",
                "project_id": PROJECT_ID,
                "reason": "QA 继续更新已有的新品发布页项目。",
            }

    if "Organizer 第二层状态整理器" in prompt:
        if _is_materials_ready(prompt):
            return {
                "item_route": "existing_item",
                "item_id": ITEM_ID,
                "event_summary": "四项客户材料已经全部交付并验收无误，材料阻塞已解除。",
                "updates": {
                    "status": "in_progress",
                    "current_progress": "页面结构、交互原型和四项客户材料均已确认；前端制作条件已满足。",
                    "next_step": "制定前端开工任务单并排期。",
                    "blocker": None,
                },
                "reason": "QA 更新了原前端制作事项并清除了材料阻塞。",
            }
        if _is_materials_blocked(prompt):
            return {
                "item_route": "new_item",
                "item": {
                    "name": "前端制作",
                    "status": "blocked",
                    "goal": "在客户材料齐备后完成新品发布页前端制作。",
                    "completion_criteria": "正式页面完成并满足交付要求。",
                    "current_progress": "页面结构和交互原型已确认；等待四项客户材料。",
                    "next_step": "材料齐备后制定前端开工任务单并排期。",
                    "blocker": "最终版 Logo、品牌色规范、产品主视觉和正式宣传文案尚未提供。",
                },
                "event_summary": "页面结构和交互原型已确认，但四项客户材料尚未齐备，前端制作保持暂停。",
                "updates": {
                    "status": "blocked",
                    "current_progress": "页面结构和交互原型已确认；等待四项客户材料。",
                    "next_step": "材料齐备后制定前端开工任务单并排期。",
                    "blocker": "最终版 Logo、品牌色规范、产品主视觉和正式宣传文案尚未提供。",
                },
                "reason": "QA 建立了一个被明确前置条件阻塞的前端制作事项。",
            }
        if _is_resume_action(prompt):
            return {
                "item_route": "existing_item",
                "item_id": ITEM_ID,
                "event_summary": "已在原 Session 生成前端开工任务单和排期草案，未知页面与负责人标记待确认。",
                "updates": {
                    "status": "in_progress",
                    "current_progress": "前端开工任务单和排期草案已生成，待补充未知页面与负责人。",
                    "next_step": "确认页面范围和负责人后开始前端制作。",
                    "blocker": "页面范围和负责人仍有待确认项。",
                },
                "reason": "本轮执行了已授权的主动推进动作。",
            }

    if "你是 Proactive Agent 的 Judge" in prompt:
        if _is_materials_ready(prompt):
            return {
                "outcome": "suggest",
                "reason": "四项开工材料刚刚全部验收，前端制作阻塞已解除，现在适合推进开工准备。",
                "title": "制定前端开工任务单并排期",
                "evidence": "四项客户材料已全部交付验收；Item blocker 已清除；交付日期临近。",
                "suggested_action": "请在当前 Session 生成一份前端开工任务单和排期草案；只使用已确认事实，未知页面和负责人标记为待确认，不调用工具。",
            }
        if _is_materials_blocked(prompt):
            return {
                "outcome": "silent",
                "reason": "前端制作仍被四项客户材料阻塞，当前没有可安全执行的开工动作。",
            }

    return {"route": "untracked", "reason": "case6 fixture received an unknown prompt"}


def main() -> int:
    try:
        prompt = sys.argv[sys.argv.index("-z") + 1]
    except (ValueError, IndexError):
        print("missing -z prompt", file=sys.stderr)
        return 2
    # Keep the demo diagnosable when a real Hermes prompt wraps the user's QA
    # in additional context.  This file is intentionally outside the service
    # contract and is only used while reproducing the interactive case.
    try:
        with Path("data/tui-case6-demo/fixture-prompts.log").open("a", encoding="utf-8") as log:
            log.write("--- prompt ---\n")
            log.write(prompt)
            log.write("\n--- end ---\n")
    except OSError:
        pass
    print(json.dumps(response_for(prompt), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
