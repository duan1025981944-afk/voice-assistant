"""桌面工具共用的 Win32 小封装（ctypes 直调，不需要管理员权限）。

文件名以下划线开头，loader 不会把它当成工具扫描。

权限说明（UIPI）
    普通权限的进程不能向"以管理员身份运行"的窗口发消息或按键。
    PostMessage 会失败，GetLastError() 是 5（拒绝访问），这里把它翻译成人话交给工具。
"""
from __future__ import annotations

import ctypes
import functools
from ctypes import wintypes
from dataclasses import dataclass

import psutil
import win32api

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

WM_CLOSE = 0x0010
SW_RESTORE = 9
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
VK_MENU = 0x12
ERROR_ACCESS_DENIED = 5

_EnumWindowsProc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
user32.EnumWindows.argtypes = [_EnumWindowsProc, wintypes.LPARAM]
user32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
user32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
user32.IsWindowVisible.argtypes = [wintypes.HWND]
user32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
user32.GetWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
user32.IsIconic.argtypes = [wintypes.HWND]
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.GetForegroundWindow.restype = wintypes.HWND
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
user32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]

GW_OWNER = 4
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_CAPTION = 0x00C00000
WS_EX_TOOLWINDOW = 0x00000080
SW_SHOW = 5
MAIN_MIN_SIZE = (200, 150)      # 比这小的窗口不当主窗口（悬浮球、托盘弹窗之类）


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    process: str      # 进程名，如 "WeChat.exe"
    exe: str = ""     # 程序完整路径，拿不到时是空的（比如以管理员身份运行的程序）
    visible: bool = True
    main: bool = True # 像不像一个主窗口：不是工具窗口，够大，有标题栏或者看得见

    @property
    def shown(self) -> bool:
        """看得见的主窗口。"""
        return self.visible and self.main

    @property
    def tucked(self) -> bool:
        """藏起来的主窗口：点了 × 之后收进托盘的软件，主窗口就是这样。"""
        return not self.visible and self.main and bool(self.title)


def list_windows() -> list[Window]:
    """列出所有顶层窗口（包括藏起来的），附属窗口和既没标题又看不见的除外。

    为什么连看不见的也要：很多软件点 × 只是把主窗口藏进托盘，进程还在。
    不看这些，就会把"在后台运行"误判成"没开"。
    """
    found: list[Window] = []
    procs: dict[int, tuple[str, str]] = {}

    def process_of(pid: int) -> tuple[str, str]:
        if pid not in procs:
            try:
                p = psutil.Process(pid)
                name = p.name()
                try:
                    exe = p.exe()
                except psutil.AccessDenied:
                    exe = ""
                procs[pid] = (name, exe)
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                procs[pid] = ("", "")
        return procs[pid]

    def callback(hwnd, _lparam):
        if user32.GetWindow(hwnd, GW_OWNER):
            return True
        visible = bool(user32.IsWindowVisible(hwnd))
        length = user32.GetWindowTextLengthW(hwnd)
        if not visible and length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)

        rect = wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        big = rect.right - rect.left >= MAIN_MIN_SIZE[0] and rect.bottom - rect.top >= MAIN_MIN_SIZE[1]
        big = big or bool(user32.IsIconic(hwnd))     # 最小化的窗口报出来只有 160×28，但它是正经主窗口
        tool = bool(user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW)
        caption = user32.GetWindowLongW(hwnd, GWL_STYLE) & WS_CAPTION == WS_CAPTION
        main = not tool and big and (visible or caption)

        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name, exe = process_of(pid.value)
        found.append(Window(hwnd=hwnd, title=buf.value, pid=pid.value, process=name,
                            exe=exe, visible=visible, main=main))
        return True

    user32.EnumWindows(_EnumWindowsProc(callback), 0)
    return found


def bring_to_front(hwnd: int) -> bool:
    """把窗口切到最前面。

    Windows 不允许后台进程随便抢焦点（SetForegroundWindow 会静默失败）。
    常用的绕法：把自己的输入线程临时挂到当前前台窗口的线程上，再切。
    """
    if user32.IsIconic(hwnd):
        user32.ShowWindow(hwnd, SW_RESTORE)
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None) if fg else 0
    me = kernel32.GetCurrentThreadId()
    attached = bool(fg_thread and fg_thread != me and user32.AttachThreadInput(me, fg_thread, True))
    try:
        user32.BringWindowToTop(hwnd)
        ok = bool(user32.SetForegroundWindow(hwnd))
        if not ok:
            # 第二招：按一下 Alt，系统会认为"用户刚有过输入"，再切一次
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
            ok = bool(user32.SetForegroundWindow(hwnd))
    finally:
        if attached:
            user32.AttachThreadInput(me, fg_thread, False)
    return ok


def show_window(hwnd: int) -> bool:
    """把藏进托盘的主窗口重新显示出来，并切到最前面。"""
    user32.ShowWindow(hwnd, SW_SHOW)
    return bring_to_front(hwnd)


@functools.lru_cache(maxsize=256)
def file_description(exe: str) -> str:
    """程序文件里写的产品名，比如 Doubao.exe → "豆包"、Weixin.exe → "微信"。拿不到就返回空串。"""
    if not exe:
        return ""
    try:
        lang, codepage = win32api.GetFileVersionInfo(exe, r"\VarFileInfo\Translation")[0]
        key = rf"\StringFileInfo\{lang:04x}{codepage:04x}\FileDescription"
        return (win32api.GetFileVersionInfo(exe, key) or "").strip()
    except Exception:
        return ""


def close_window(hwnd: int) -> str | None:
    """发 WM_CLOSE（相当于点右上角的 ×）。成功返回 None，失败返回原因。"""
    if user32.PostMessageW(hwnd, WM_CLOSE, 0, 0):
        return None
    if ctypes.get_last_error() == ERROR_ACCESS_DENIED:
        return "这个窗口是以管理员身份运行的，普通权限关不掉它"
    return f"关闭失败（错误码 {ctypes.get_last_error()}）"


def press_key(vk: int) -> None:
    """按一下再松开一个虚拟键（媒体键要带 EXTENDEDKEY 标志）。"""
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    user32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
