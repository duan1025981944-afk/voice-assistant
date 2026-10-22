"""屏幕亮度。笔记本内屏走 WMI，不需要管理员权限；外接显示器要在显示器菜单里开 DDC/CI。"""
from __future__ import annotations

import screen_brightness_control as sbc

from agent.tools.base import Tool, ToolResult


class BrightnessTool(Tool):
    name = "brightness"
    description = (
        "查看或调整屏幕亮度（主屏幕）。"
        "set 设成指定值；up/down 加减（默认 10）；query 只查看。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "up", "down", "query"]},
            "amount": {
                "type": "integer",
                "description": "set 时是目标亮度 0–100；up/down 时是加减多少，不填默认 10",
            },
        },
        "required": ["action"],
    }
    needs_confirm = False

    async def execute(self, action: str, amount: int | None = None) -> str:
        try:
            current = sbc.get_brightness(display=0)[0]
            if action == "query":
                return f"当前亮度 {current}%"
            if action == "set":
                if amount is None:
                    return ToolResult.error("set 需要 amount（目标亮度 0–100）")
                target = amount
            elif action in ("up", "down"):
                step = amount if amount is not None else 10
                target = current + step if action == "up" else current - step
            else:
                return ToolResult.error(f"不认识的 action：{action}")
            target = max(0, min(100, int(target)))
            sbc.set_brightness(target, display=0)
            return f"亮度已调到 {target}%"
        except Exception as exc:     # sbc 在不支持的屏幕上会抛各种类型的异常
            return ToolResult.error(f"这块屏幕调不了亮度（外接显示器需要开启 DDC/CI）：{exc}")
