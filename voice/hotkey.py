"""全局快捷键：Win32 RegisterHotKey。

为什么不用 keyboard 之类的库
    它们装的是"低级键盘钩子"，能看到你按的每一个键——这正是键盘记录器的做法，
    杀毒软件容易报警。RegisterHotKey 只让系统在"这一个组合键"被按下时通知我们，
    不需要管理员权限，也看不到别的按键。

限制
    组合键已经被别的软件占用时注册会失败，这时换一个（config.json 的 voice.hotkey）。
"""
from __future__ import annotations

import ctypes
import logging
import threading
from ctypes import wintypes
from typing import Callable

log = logging.getLogger(__name__)

user32 = ctypes.WinDLL("user32", use_last_error=True)

MOD = {"alt": 0x1, "ctrl": 0x2, "control": 0x2, "shift": 0x4, "win": 0x8}
MOD_NOREPEAT = 0x4000
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
NAMED_KEYS = {"space": 0x20, "enter": 0x0D, "tab": 0x09, "esc": 0x1B, "backquote": 0xC0, "`": 0xC0}


def parse(spec: str) -> tuple[int, int]:
    """"ctrl+alt+space" → (修饰键位, 虚拟键码)

    >>> parse("ctrl+alt+space")
    (3, 32)
    >>> parse("win+alt+space")
    (9, 32)
    >>> parse("Win+Shift+F9")
    (12, 120)
    """
    *mods, key = [p.strip().lower() for p in spec.split("+")]
    try:
        modifiers = sum(MOD[m] for m in mods)
    except KeyError as exc:
        raise ValueError(f"不认识的修饰键 {exc}，可用：ctrl / alt / shift / win") from None
    if key in NAMED_KEYS:
        vk = NAMED_KEYS[key]
    elif len(key) == 1 and key.isalnum():
        vk = ord(key.upper())
    elif key.startswith("f") and key[1:].isdigit() and 1 <= int(key[1:]) <= 24:
        vk = 0x70 + int(key[1:]) - 1
    else:
        raise ValueError(f"不认识的按键：{key}")
    return modifiers, vk


class Hotkey(threading.Thread):
    """独立线程跑一个最小的 Windows 消息循环，收到 WM_HOTKEY 就调 on_press。"""

    def __init__(self, spec: str, on_press: Callable[[], None]):
        super().__init__(name="hotkey", daemon=True)
        self.spec = spec
        self.modifiers, self.vk = parse(spec)
        self.on_press = on_press
        self.ready = threading.Event()
        self.error: str | None = None

    def run(self) -> None:
        # 热键属于注册它的线程，所以注册和消息循环必须在同一个线程里
        if not user32.RegisterHotKey(None, 1, self.modifiers | MOD_NOREPEAT, self.vk):
            self.error = f"快捷键 {self.spec} 注册失败（可能被别的软件占用了），换一个试试"
            self.ready.set()
            return
        self.ready.set()
        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
                if msg.message == WM_HOTKEY:
                    try:
                        self.on_press()
                    except Exception:
                        log.exception("快捷键回调出错")
        finally:
            user32.UnregisterHotKey(None, 1)

    def stop(self) -> None:
        if self.native_id:
            user32.PostThreadMessageW(self.native_id, WM_QUIT, 0, 0)
