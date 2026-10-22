"""托盘：她的头像常驻右下角，左键右键点一下都会弹出圆角菜单。

──────────────────────────────────────────────────────────────
为什么不用 pystray 了
──────────────────────────────────────────────────────────────
pystray 右键只能弹 Windows 原生菜单，字号、间距、颜色都改不了。
这里自己用 Shell_NotifyIcon 放托盘图标，点击时弹一个 tkinter 画的小窗口当菜单；
圆角和阴影交给 Windows 11 的 DWM 画（Windows 10 上是直角，其余一样）。

──────────────────────────────────────────────────────────────
线程
──────────────────────────────────────────────────────────────
    托盘线程   托盘图标背后的隐藏窗口 + 它自己的消息循环。图标被点了，只往队列里放一个标记
    主线程     tkinter：每 50ms 看一眼队列，有点击就开关菜单，有状态变化就换图标

为什么点击不直接在托盘线程里开菜单：tkinter 只能在创建它的那个线程里用。
早先试过把隐藏窗口也放进 tkinter 的事件循环里，在回调里碰 tkinter 会让 Python 直接崩溃。
"""
from __future__ import annotations

import ctypes
import os
import queue
import threading
import time
import tkinter as tk
from datetime import date

import win32api
import win32con
import win32gui
from PIL import ImageTk

from assistant import Assistant, State
from ui import icons
from voice.paths import DATA_DIR

WM_TRAY = win32con.WM_USER + 20

FONT = "Microsoft YaHei UI"
BG = "#ffffff"
HOVER = "#f0f3f7"
TEXT = "#1f2328"
MUTED = "#8b929c"
LINE = "#e2eaf4"

STATUS_TEXT = {
    State.IDLE: "待命中",
    State.LISTENING: "在听…",
    State.THINKING: "在想…",
    State.SPEAKING: "在说话",
    State.PAUSED: "已暂停，麦克风已关闭",
}

user32 = ctypes.windll.user32
dwmapi = ctypes.windll.dwmapi
DWMWA_WINDOW_CORNER_PREFERENCE = 33
DWMWA_BORDER_COLOR = 34
DWMWCP_ROUND = 2
GCL_STYLE = -26
CS_DROPSHADOW = 0x00020000
VK_LBUTTON, VK_RBUTTON = 0x01, 0x02


def place_popup(cursor: tuple[int, int], size: tuple[int, int], work: tuple[int, int, int, int]) -> tuple[int, int]:
    """菜单放哪：右下角对着鼠标（任务栏在下面时就在鼠标左上方），不超出屏幕工作区。

    >>> place_popup((1900, 1060), (240, 300), (0, 0, 1920, 1040))   # 任务栏在底部
    (1660, 740)
    >>> place_popup((1900, 10), (240, 300), (0, 40, 1920, 1080))    # 任务栏在顶部
    (1660, 40)
    """
    cx, cy = cursor
    w, h = size
    left, top, right, bottom = work
    x = min(max(cx - w, left), right - w)
    y = cy - h if cy - h >= top else cy
    y = min(max(y, top), bottom - h)
    return x, y


def pretty_hotkey(spec: str) -> str:
    """配置里的写法 → 给人看的写法。

    >>> pretty_hotkey("win+alt+space")
    'Win + Alt + Space'
    """
    return " + ".join(p.strip().capitalize() for p in spec.split("+"))


def turns_today() -> int:
    path = DATA_DIR / "events" / f"{date.today():%Y%m%d}.jsonl"
    if not path.exists():
        return 0
    with open(path, encoding="utf-8") as f:
        return sum(1 for line in f if line.strip())


class NotifyIcon(threading.Thread):
    """托盘里的那个图标：放上去、换图、点击回调、资源管理器重启后自动放回去。

    set() / remove() 可以在任何线程调；点击回调在托盘线程里触发，别在里面碰 tkinter。
    """

    def __init__(self, on_click):
        super().__init__(name="tray", daemon=True)
        self.on_click = on_click
        self.hwnd = None
        self._hicon = None
        self._tip = ""
        self._added = False
        self._ready = threading.Event()

    def run(self) -> None:
        taskbar_created = win32gui.RegisterWindowMessage("TaskbarCreated")
        wc = win32gui.WNDCLASS()
        wc.hInstance = win32api.GetModuleHandle(None)
        wc.lpszClassName = "VoiceAssistantTray"
        wc.lpfnWndProc = {
            WM_TRAY: self._on_tray,
            taskbar_created: self._on_taskbar_created,
            win32con.WM_CLOSE: lambda hwnd, *_: win32gui.DestroyWindow(hwnd) or 0,
            win32con.WM_DESTROY: lambda *_: win32gui.PostQuitMessage(0) or 0,
        }
        win32gui.RegisterClass(wc)
        self.hwnd = win32gui.CreateWindow(
            wc.lpszClassName, "VoiceAssistantTray", 0, 0, 0, 0, 0, 0, 0, wc.hInstance, None
        )
        self._ready.set()
        win32gui.PumpMessages()

    def set(self, ico_path, tip: str) -> None:
        self._ready.wait()
        size = win32api.GetSystemMetrics(win32con.SM_CXSMICON)     # 跟着系统缩放：100% 是 16，150% 是 24
        hicon = win32gui.LoadImage(
            0, str(ico_path), win32con.IMAGE_ICON, size, size, win32con.LR_LOADFROMFILE
        )
        old, self._hicon, self._tip = self._hicon, hicon, tip[:127]
        self._notify(win32gui.NIM_MODIFY if self._added else win32gui.NIM_ADD)
        self._added = True
        if old:
            win32gui.DestroyIcon(old)

    def remove(self) -> None:
        if self._added:
            win32gui.Shell_NotifyIcon(win32gui.NIM_DELETE, (self.hwnd, 0))
            self._added = False
        if self.hwnd:
            win32gui.PostMessage(self.hwnd, win32con.WM_CLOSE, 0, 0)

    def _notify(self, message) -> None:
        flags = win32gui.NIF_ICON | win32gui.NIF_MESSAGE | win32gui.NIF_TIP
        win32gui.Shell_NotifyIcon(message, (self.hwnd, 0, flags, WM_TRAY, self._hicon, self._tip))

    def _on_tray(self, hwnd, msg, wparam, lparam):
        if lparam in (win32con.WM_LBUTTONUP, win32con.WM_RBUTTONUP):
            self.on_click()
        return 0

    def _on_taskbar_created(self, hwnd, msg, wparam, lparam):
        # explorer.exe 重启（或崩溃）后任务栏是新的，托盘图标要重新放一次
        if self._hicon:
            self._notify(win32gui.NIM_ADD)
        return 0


class PopupMenu:
    """点托盘图标弹出来的圆角菜单。每次打开都重新画，信息总是最新的。"""

    def __init__(self, root: tk.Tk, tray: "Tray"):
        self.root = root
        self.tray = tray
        self.win: tk.Toplevel | None = None
        self._hwnd = 0
        self._status: tk.Label | None = None
        self._dot: tk.Label | None = None
        self._avatar = None
        self._closed_at = 0.0
        scaling = float(root.tk.call("tk", "scaling"))             # 每"点"多少像素，跟着系统缩放
        self.scale = scaling * 72 / 96

    def px(self, n: float) -> int:
        return int(round(n * self.scale))

    def font(self, size: float, *style: str) -> tuple:
        return (FONT, -self.px(size), *style)                     # 负数 = 像素，和间距一起按缩放比例走

    # ── 打开 / 关闭 ───────────────────────────────────────

    def toggle(self) -> None:
        if self.win is not None:
            self.close()
        elif time.monotonic() - self._closed_at > 0.4:
            # 刚因为"在菜单外按下鼠标"关掉——那一下多半就是点在图标上，别马上又打开
            self.open()

    def open(self) -> None:
        a = self.tray.assistant
        win = tk.Toplevel(self.root, bg=BG)
        win.withdraw()
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        self.win = win

        body = tk.Frame(win, bg=BG, pady=self.px(6))
        body.pack(fill="both", expand=True)
        self._header(body, a)
        self._line(body)
        self._item(body, "恢复监听" if a.state is State.PAUSED else "暂停监听", self.tray.toggle_pause)
        self._item(body, "打开日志文件夹", lambda: os.startfile(DATA_DIR))
        self._line(body)
        self._info(body, f"唤醒词：{'、'.join(a.settings.wake_words)}")
        self._info(body, f"快捷键：{pretty_hotkey(a.settings.hotkey)}")
        self._info(body, f"今天对话：{turns_today()} 次")
        self._line(body)
        self._item(body, "退出", self.tray.quit)

        win.update_idletasks()
        size = (max(win.winfo_reqwidth(), self.px(240)), win.winfo_reqheight())
        cursor = win32api.GetCursorPos()
        monitor = win32api.MonitorFromPoint(cursor, win32con.MONITOR_DEFAULTTONEAREST)
        work = win32api.GetMonitorInfo(monitor)["Work"]
        x, y = place_popup(cursor, size, work)
        win.geometry(f"{size[0]}x{size[1]}+{x}+{y}")
        win.deiconify()

        self._hwnd = user32.GetParent(win.winfo_id())
        self._decorate(self._hwnd)
        win.lift()
        win.focus_force()
        user32.SetForegroundWindow(self._hwnd)
        win.bind("<Escape>", lambda e: self.close())
        self.root.after(100, self._watch)

    def close(self) -> None:
        if self.win is not None:
            self.win.destroy()
            self._closed_at = time.monotonic()
        self.win = self._status = self._dot = None

    def _watch(self) -> None:
        """在菜单外面按下鼠标就关。不靠焦点判断：托盘程序抢前台经常被 Windows 拒绝。"""
        if self.win is None:
            return
        pressed = any(user32.GetAsyncKeyState(vk) & 0x8000 for vk in (VK_LBUTTON, VK_RBUTTON))
        if pressed:
            x, y = win32api.GetCursorPos()
            left, top = self.win.winfo_rootx(), self.win.winfo_rooty()
            inside = left <= x < left + self.win.winfo_width() and top <= y < top + self.win.winfo_height()
            if not inside:
                self.close()
                return
        self.root.after(50, self._watch)

    def _decorate(self, hwnd: int) -> None:
        """Windows 11：圆角 + 浅灰描边 + 阴影。旧系统上这些调用会失败，忽略即可。"""
        corner = ctypes.c_int(DWMWCP_ROUND)
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_WINDOW_CORNER_PREFERENCE, ctypes.byref(corner), 4)
        border = ctypes.c_uint(0x00E3E0DE)                       # COLORREF 是 BGR 顺序
        dwmapi.DwmSetWindowAttribute(hwnd, DWMWA_BORDER_COLOR, ctypes.byref(border), 4)
        user32.GetClassLongPtrW.restype = ctypes.c_size_t
        user32.SetClassLongPtrW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_size_t]
        style = user32.GetClassLongPtrW(hwnd, GCL_STYLE)
        user32.SetClassLongPtrW(hwnd, GCL_STYLE, style | CS_DROPSHADOW)

    # ── 菜单里的几种行 ────────────────────────────────────

    def _header(self, parent, a: Assistant) -> None:
        row = tk.Frame(parent, bg=BG, padx=self.px(18), pady=self.px(10))
        row.pack(fill="x")
        self._avatar = ImageTk.PhotoImage(icons.round_avatar(self.px(40)))
        tk.Label(row, image=self._avatar, bg=BG).pack(side="left")
        text = tk.Frame(row, bg=BG, padx=self.px(12))
        text.pack(side="left", fill="x")
        tk.Label(text, text=a.settings.assistant_name, bg=BG, fg=TEXT,
                 font=self.font(15, "bold"), anchor="w").pack(fill="x")
        status = tk.Frame(text, bg=BG)
        status.pack(fill="x")
        self._dot = tk.Label(status, text="●", bg=BG, font=self.font(10))
        self._dot.pack(side="left")
        self._status = tk.Label(status, bg=BG, fg=MUTED, font=self.font(12), anchor="w")
        self._status.pack(side="left")
        self.show_state(a.state)

    def show_state(self, state: State) -> None:
        """菜单开着时状态变了，顺手更新那一行。"""
        if self._status is None:
            return
        color = icons.DOT_COLORS.get(state, (160, 166, 175))
        self._dot.configure(fg="#%02x%02x%02x" % color)
        self._status.configure(text=" " + STATUS_TEXT[state])

    def _item(self, parent, label: str, action) -> None:
        outer = tk.Frame(parent, bg=BG, padx=self.px(6))
        outer.pack(fill="x")
        cell = tk.Frame(outer, bg=BG, padx=self.px(14), pady=self.px(8), cursor="hand2")
        cell.pack(fill="x")
        text = tk.Label(cell, text=label, bg=BG, fg=TEXT, font=self.font(14), anchor="w", cursor="hand2")
        text.pack(fill="x")

        def paint(color):
            cell.configure(bg=color)
            text.configure(bg=color)

        def run(_event):
            self.close()
            self.root.after(10, action)

        for widget in (cell, text):
            widget.bind("<Enter>", lambda e: paint(HOVER))
            widget.bind("<Leave>", lambda e: paint(BG))
            widget.bind("<ButtonRelease-1>", run)

    def _info(self, parent, label: str) -> None:
        tk.Label(parent, text=label, bg=BG, fg=MUTED, font=self.font(13), anchor="w",
                 padx=self.px(20), pady=self.px(4)).pack(fill="x")

    def _line(self, parent) -> None:
        tk.Frame(parent, bg=LINE, height=max(1, self.px(1))).pack(fill="x", pady=self.px(6))


class Tray:
    def __init__(self, assistant: Assistant):
        _make_dpi_aware()                       # 必须在创建任何窗口之前，不然高分屏上发虚
        self.assistant = assistant
        self.root = tk.Tk()
        self.root.withdraw()
        self.icons = icons.write_state_icons()
        self.menu = PopupMenu(self.root, self)
        self._events: queue.Queue = queue.Queue()        # 别的线程只往这里放东西，不碰界面
        self.icon = NotifyIcon(on_click=lambda: self._events.put("click"))
        self.icon.start()
        self._show(assistant.state)
        assistant.on_state = self._events.put

    def run(self) -> None:
        self.root.after(50, self._poll)
        self.root.mainloop()                    # 阻塞，直到点"退出"

    def _poll(self) -> None:
        latest_state = None
        while not self._events.empty():
            event = self._events.get_nowait()
            if event == "click":
                self.menu.toggle()
            else:
                latest_state = event
        if latest_state is not None:
            self._show(latest_state)
        self.root.after(50, self._poll)

    def _show(self, state: State) -> None:
        name = self.assistant.settings.assistant_name
        self.icon.set(self.icons[state], f"{name}：{state.value}")
        self.menu.show_state(state)

    def toggle_pause(self) -> None:
        if self.assistant.state is State.PAUSED:
            self.assistant.resume()
        else:
            self.assistant.pause()

    def quit(self) -> None:
        self.menu.close()
        self.assistant.shutdown()
        self.icon.remove()
        self.root.destroy()


def _make_dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)      # 按系统缩放比例绘制
    except (AttributeError, OSError):
        user32.SetProcessDPIAware()
