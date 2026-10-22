"""托盘图标：颜色表示状态，右键菜单可以暂停监听、打开日志、退出。左键点一下 = 暂停/恢复。"""
from __future__ import annotations

import os

import pystray
from PIL import Image, ImageDraw

from assistant import Assistant, State
from voice.paths import DATA_DIR

COLORS = {
    State.IDLE: (140, 140, 140),
    State.LISTENING: (39, 174, 96),
    State.THINKING: (41, 128, 185),
    State.SPEAKING: (230, 126, 34),
    State.PAUSED: (192, 57, 43),
}


def icon_image(state: State) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    draw.ellipse((4, 4, 60, 60), fill=COLORS[state])
    # 一个简笔麦克风：话筒 + 支架
    draw.rounded_rectangle((25, 14, 39, 38), radius=7, fill="white")
    draw.arc((19, 24, 45, 46), start=0, end=180, fill="white", width=3)
    draw.line((32, 46, 32, 52), fill="white", width=3)
    if state is State.PAUSED:
        draw.line((16, 16, 48, 48), fill="white", width=4)
    return img


class Tray:
    def __init__(self, assistant: Assistant):
        self.assistant = assistant
        self.name = assistant.settings.assistant_name
        self.icon = pystray.Icon(
            "voice-assistant",
            icon_image(assistant.state),
            self._title(assistant.state),
            menu=pystray.Menu(
                pystray.MenuItem(
                    lambda item: "恢复监听" if self.assistant.state is State.PAUSED else "暂停监听",
                    self._toggle, default=True,
                ),
                pystray.MenuItem("打开日志文件夹", self._open_logs),
                pystray.MenuItem("退出", self._quit),
            ),
        )
        assistant.on_state = self.update

    def _title(self, state: State) -> str:
        return f"{self.name}：{state.value}"

    def update(self, state: State) -> None:
        self.icon.icon = icon_image(state)
        self.icon.title = self._title(state)

    def run(self) -> None:
        self.icon.run()           # 阻塞，直到点"退出"

    def _toggle(self, icon, item) -> None:
        if self.assistant.state is State.PAUSED:
            self.assistant.resume()
        else:
            self.assistant.pause()

    def _open_logs(self, icon, item) -> None:
        os.startfile(DATA_DIR)

    def _quit(self, icon, item) -> None:
        self.assistant.shutdown()
        icon.stop()
