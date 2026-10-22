"""右下角的进度气泡：做到哪一步了、已经等了几秒，一轮结束时显示用时、token 和花费。

    在听…                     ← 唤醒后
    听到「打开微信」，在想…     ← 识别完，开始调模型
    正在打开 微信              ← 模型决定调工具
    准备说话… / 在说：好的      ← 合成语音 / 开始出声
    等了 3.8 秒 · 2,345 token · 0.0021 元   ← 结束，4 秒后自动消失

不抢焦点：窗口带 WS_EX_NOACTIVATE，弹出来时你正在打的字不会被打断。
点一下气泡就收起来。config.json 里 voice.showStatus 设成 false 就不显示。
"""
from __future__ import annotations

import ctypes
import time
import tkinter as tk

import win32api
import win32con
from PIL import ImageTk

from assistant import Progress
from ui import icons

BG = "#ffffff"
TEXT = "#1f2328"
MUTED = "#8b929c"
HIDE_AFTER_DONE = 4.0            # 一轮结束后，气泡再停留几秒

user32 = ctypes.windll.user32
GWL_EXSTYLE = -20
WS_EX_NOACTIVATE = 0x08000000
WS_EX_TOOLWINDOW = 0x00000080
OFFSCREEN = "+-32000+-32000"


class StatusBubble:
    """一直存在的小窗口：要显示时挪到右下角，不显示时挪到屏幕外、透明。

    为什么不每次 withdraw / deiconify：tkinter 重新显示窗口时会把它激活，抢走你的输入焦点。
    挪位置和改透明度不会。
    """

    def __init__(self, root: tk.Tk, px, font, decorate):
        self.root = root
        self.px = px
        self.win = tk.Toplevel(root, bg=BG)
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", 0.0)
        self.win.geometry(OFFSCREEN)

        row = tk.Frame(self.win, bg=BG, padx=px(14), pady=px(10))
        row.pack(fill="both", expand=True)
        self._avatar = ImageTk.PhotoImage(icons.round_avatar(px(30)))
        tk.Label(row, image=self._avatar, bg=BG).pack(side="left", anchor="n")
        text = tk.Frame(row, bg=BG, padx=px(10))
        text.pack(side="left", fill="both", expand=True)
        self.step = tk.Label(text, bg=BG, fg=TEXT, font=font(14), anchor="w", justify="left",
                             wraplength=px(260))
        self.step.pack(fill="x")
        self.detail = tk.Label(text, bg=BG, fg=MUTED, font=font(12), anchor="w")
        self.detail.pack(fill="x")
        for widget in (self.win, row, text, self.step, self.detail):
            widget.bind("<Button-1>", lambda e: self.hide())

        self.win.update_idletasks()
        self.hwnd = user32.GetParent(self.win.winfo_id())
        style = user32.GetWindowLongW(self.hwnd, GWL_EXSTYLE)
        user32.SetWindowLongW(self.hwnd, GWL_EXSTYLE, style | WS_EX_NOACTIVATE | WS_EX_TOOLWINDOW)
        decorate(self.hwnd)

        self._started = 0.0        # 这一轮从什么时候开始计时
        self._running = False      # 还在进行中（显示"已用 x 秒"并持续刷新）
        self._hide_at = 0.0
        self.visible = False

    def show(self, progress: Progress) -> None:
        now = time.monotonic()
        if progress.start or not self.visible and not self._running:
            self._started = now
        self._running = not progress.done
        self._hide_at = now + HIDE_AFTER_DONE if progress.done else 0.0
        self.step.configure(text=progress.text)
        self._refresh_detail(now)
        self._place()

    def tick(self) -> None:
        """托盘的轮询每 50ms 调一次：刷新秒数，到时间就收起来。"""
        if not self.visible:
            return
        now = time.monotonic()
        if self._hide_at and now >= self._hide_at:
            self.hide()
        elif self._running:
            self._refresh_detail(now)

    def hide(self) -> None:
        self.win.attributes("-alpha", 0.0)
        self.win.geometry(OFFSCREEN)
        self.visible = False
        self._running = False

    def _refresh_detail(self, now: float) -> None:
        self.detail.configure(text=f"已用 {now - self._started:.1f} 秒" if self._running else "")

    def _place(self) -> None:
        self.win.update_idletasks()
        w = max(self.win.winfo_reqwidth(), self.px(300))
        h = self.win.winfo_reqheight()
        # 托盘所在的主显示器（包含坐标 (0,0) 的那块），贴着任务栏上方的右下角
        work = win32api.GetMonitorInfo(win32api.MonitorFromPoint((0, 0), win32con.MONITOR_DEFAULTTOPRIMARY))["Work"]
        margin = self.px(12)
        self.win.geometry(f"{w}x{h}+{work[2] - w - margin}+{work[3] - h - margin}")
        self.win.attributes("-alpha", 1.0)
        self.visible = True
