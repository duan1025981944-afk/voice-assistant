"""现在几点、今天几号。模型自己不知道当前时间，得问这个工具。"""
from __future__ import annotations

from datetime import datetime

from agent.tools.base import Tool

WEEKDAYS = "一二三四五六日"


class ClockTool(Tool):
    name = "clock"
    description = "查当前的日期、星期和时间。用户问'几点了''今天几号''星期几'时用。"
    parameters = {"type": "object", "properties": {}}
    read_only = True

    async def execute(self) -> str:
        now = datetime.now()
        return f"{now:%Y年%m月%d日} 星期{WEEKDAYS[now.weekday()]} {now:%H:%M}"
