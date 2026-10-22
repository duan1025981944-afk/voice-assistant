"""锁屏、睡眠、关机、重启、取消关机。都不需要管理员权限。

关机和重启要先问用户（requires_confirm 按 action 判断），而且不是立刻执行：
shutdown /t 60 留 60 秒，期间说"取消关机"就会执行 shutdown /a。
"""
from __future__ import annotations

import ctypes
import subprocess
import threading

from agent.tools.base import Tool, ToolResult

DELAY_SECONDS = 60
CONFIRM_ACTIONS = {"shutdown", "restart"}

# shutdown.exe 的返回码
ALREADY_SCHEDULED = 1190
NOTHING_TO_ABORT = 1116


def _run(args: list[str]) -> int:
    return subprocess.run(
        ["shutdown", *args], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW
    ).returncode


def _sleep_now() -> None:
    # SetSuspendState(休眠?, 强制?, 禁止唤醒事件?)：第一个参数 False 才是睡眠
    ctypes.WinDLL("powrprof").SetSuspendState(False, False, False)


class PowerTool(Tool):
    name = "power"
    description = (
        "lock 锁屏；sleep 睡眠（3 秒后）；shutdown 关机、restart 重启（都会留 60 秒倒计时）；"
        "cancel 取消已经安排的关机或重启。"
        "关机和重启会由程序先向用户确认，你不需要再问。"
    )
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": ["lock", "sleep", "shutdown", "restart", "cancel"]}},
        "required": ["action"],
    }

    def requires_confirm(self, args: dict) -> bool:
        return args.get("action") in CONFIRM_ACTIONS

    def confirm_prompt(self, args: dict) -> str:
        what = "重启" if args.get("action") == "restart" else "关机"
        return f"确定要{what}吗？"

    async def execute(self, action: str) -> str:
        if action == "lock":
            ctypes.windll.user32.LockWorkStation()
            return "已锁屏"
        if action == "sleep":
            threading.Timer(3, _sleep_now).start()      # 留时间把"好的"说完
            return "3 秒后睡眠"
        if action in CONFIRM_ACTIONS:
            flag, what = ("/r", "重启") if action == "restart" else ("/s", "关机")
            note = f"语音助手：{DELAY_SECONDS} 秒后{what}。说“取消关机”可以撤销。"
            code = _run([flag, "/t", str(DELAY_SECONDS), "/c", note])
            if code == ALREADY_SCHEDULED:
                return ToolResult.error("已经安排过关机或重启了，要改的话先 cancel")
            if code != 0:
                return ToolResult.error(f"shutdown 命令失败，返回码 {code}")
            return f"{DELAY_SECONDS} 秒后{what}，说取消关机可以撤销"
        if action == "cancel":
            code = _run(["/a"])
            if code == NOTHING_TO_ABORT:
                return "现在没有安排关机或重启"
            if code != 0:
                return ToolResult.error(f"取消失败，返回码 {code}")
            return "已取消关机"
        return ToolResult.error(f"不认识的 action：{action}")
