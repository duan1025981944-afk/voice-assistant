"""系统音量（默认播放设备）。走 Core Audio 的 COM 接口，不需要管理员权限。"""
from __future__ import annotations

import comtypes
from pycaw.pycaw import AudioUtilities

from agent.tools.base import Tool, ToolResult


def _endpoint():
    # COM 要求每个线程先初始化一次；助手的大脑跑在自己的线程里，所以这里补上。
    # 同一线程重复调用是无害的。
    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    return AudioUtilities.GetSpeakers().EndpointVolume


def get_volume() -> tuple[int, bool]:
    """(当前音量 0–100, 是否静音)"""
    ep = _endpoint()
    return round(ep.GetMasterVolumeLevelScalar() * 100), bool(ep.GetMute())


def set_volume(level: int) -> int:
    level = max(0, min(100, int(level)))
    ep = _endpoint()
    ep.SetMasterVolumeLevelScalar(level / 100, None)
    return level


def set_mute(muted: bool) -> None:
    _endpoint().SetMute(int(muted), None)


class VolumeTool(Tool):
    name = "volume"
    description = (
        "查看或调整电脑的系统音量、静音。"
        "set 设成指定值；up/down 在当前基础上加减（用户说'大一点''小一点'时用，默认 10）；"
        "mute/unmute 静音和取消静音；query 只查看。"
        "不是控制音乐播放（那用 media）。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["set", "up", "down", "mute", "unmute", "query"]},
            "amount": {
                "type": "integer",
                "description": "set 时是目标音量 0–100；up/down 时是加减多少，不填默认 10",
            },
        },
        "required": ["action"],
    }
    needs_confirm = False

    async def execute(self, action: str, amount: int | None = None) -> str:
        try:
            current, muted = get_volume()
            if action == "query":
                return f"当前音量 {current}%" + ("，静音中" if muted else "")
            if action in ("mute", "unmute"):
                set_mute(action == "mute")
                return "已静音" if action == "mute" else f"已取消静音，音量 {current}%"
            if action == "set":
                if amount is None:
                    return ToolResult.error("set 需要 amount（目标音量 0–100）")
                target = amount
            elif action in ("up", "down"):
                step = amount if amount is not None else 10
                target = current + step if action == "up" else current - step
            else:
                return ToolResult.error(f"不认识的 action：{action}")
            level = set_volume(target)
            if muted and level > 0:
                set_mute(False)          # 想调大声却还静音着，没有意义
            return f"音量已调到 {level}%"
        except OSError as exc:
            return ToolResult.error(f"读写音量失败，可能没有播放设备：{exc}")
