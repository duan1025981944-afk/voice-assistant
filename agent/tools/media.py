"""媒体控制：模拟键盘上的媒体键，对网易云、QQ 音乐、浏览器里的视频等都有效。"""
from __future__ import annotations

from agent.tools import _win32
from agent.tools.base import Tool, ToolResult

VK = {
    "play_pause": 0xB3,   # VK_MEDIA_PLAY_PAUSE
    "next": 0xB0,         # VK_MEDIA_NEXT_TRACK
    "previous": 0xB1,     # VK_MEDIA_PREV_TRACK
    "stop": 0xB2,         # VK_MEDIA_STOP
}
DONE = {"play_pause": "已切换播放/暂停", "next": "已切到下一首", "previous": "已切到上一首", "stop": "已停止播放"}


class MediaTool(Tool):
    name = "media"
    description = (
        "控制正在播放的音乐或视频：play_pause 播放/暂停（同一个键，播放中按就暂停），"
        "next 下一首，previous 上一首，stop 停止。"
        "调音量用 volume，不用这个。"
    )
    parameters = {
        "type": "object",
        "properties": {"action": {"type": "string", "enum": list(VK)}},
        "required": ["action"],
    }
    needs_confirm = False

    async def execute(self, action: str) -> str:
        if action not in VK:
            return ToolResult.error(f"不认识的 action：{action}")
        _win32.press_key(VK[action])
        return DONE[action]
