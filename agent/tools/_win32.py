"""桌面工具共用的 Win32 小封装（ctypes 直调，不需要管理员权限）。

文件名以下划线开头，loader 不会把它当成工具扫描。

权限说明（UIPI）
    普通权限的进程不能向"以管理员身份运行"的窗口发消息或按键。
    PostMessage 会失败，GetLastError() 是 5（拒绝访问），这里把它翻译成人话交给工具。
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass

import psutil

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

GW_OWNER = 4


@dataclass
class Window:
    hwnd: int
    title: str
    pid: int
    process: str      # 进程名，如 "WeChat.exe"


def list_windows() -> list[Window]:
    """列出任务栏上能看到的顶层窗口（可见、有标题、不是别的窗口的附属窗口）。"""
    found: list[Window] = []

    def callback(hwnd, _lparam):
        if not user32.IsWindowVisible(hwnd) or user32.GetWindow(hwnd, GW_OWNER):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        try:
            process = psutil.Process(pid.value).name()
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            process = ""
        found.append(Window(hwnd=hwnd, title=buf.value, pid=pid.value, process=process))
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
