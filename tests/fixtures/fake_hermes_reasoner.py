#!/usr/bin/env python3
"""Deterministic semantic worker used by the live bridge acceptance test.

It implements only the JSON protocol consumed by ``HermesJsonReasoner``.  The
interactive side of the test still runs the real Hermes TUI; this worker keeps
Organizer/Judge latency predictable so the acceptance test can assert the
complete approve -> resume -> turn.completed path.
"""

from __future__ import annotations

import json
import sys


def response_for(prompt: str) -> dict[str, object]:
    if "Organizer 第一层路由器" in prompt:
        return {
            "route": "new_project",
            "project_name": "Hermes TUI 验收",
            "project_summary": "验证 Proactive Agent 在 Hermes TUI 中的建议与同 Session 续跑。",
            "reason": "当前 QA 明确描述一项可追踪的 TUI 验收目标。",
        }

    if "Organizer 第二层状态整理器" in prompt:
        return {
            "item_route": "new_item",
            "item": {
                "name": "建议接受后续跑",
                "status": "in_progress",
                "goal": "确认接受建议后原 Session 能继续执行。",
                "completion_criteria": "同一 Session 收到建议动作并返回明确确认。",
                "current_progress": "已显示建议框，等待接受后续跑。",
                "next_step": "接受建议并让原 Session 只回复续跑成功。",
                "blocker": None,
            },
            "event_summary": "已显示建议框，等待接受后续跑。",
            "updates": {
                "status": "in_progress",
                "current_progress": "已显示建议框，等待接受后续跑。",
                "next_step": "接受建议并让原 Session 只回复续跑成功。",
                "blocker": None,
            },
            "reason": "QA 建立了一个独立的验收事项。",
        }

    if "你是 Proactive Agent 的 Judge" in prompt:
        return {
            "outcome": "suggest",
            "reason": "下一步明确且适合由用户授权后执行。",
            "title": "验证同一 Session 续跑",
            "evidence": "next_step=接受建议并让原 Session 只回复续跑成功。",
            "suggested_action": "只回复：同一 Session 续跑成功，不调用工具。",
        }

    # The service should not ask the worker for any other operation in this
    # fixture. Returning a valid untracked route keeps failures explicit.
    return {"route": "untracked", "reason": "fixture received an unknown prompt"}


def main() -> int:
    try:
        prompt = sys.argv[sys.argv.index("-z") + 1]
    except (ValueError, IndexError):
        print("missing -z prompt", file=sys.stderr)
        return 2

    print(json.dumps(response_for(prompt), ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
