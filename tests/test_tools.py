"""桌面工具。默认不碰真实设置；标了 system 的会读写音量，测完恢复原样。"""
import asyncio

import pytest

from agent.tools import apps, power, volume
from agent.tools.loader import discover_tools


def run(coro):
    return asyncio.run(coro)


def test_all_tools_discovered():
    tools = discover_tools()
    assert set(tools) == {
        "brightness", "clock", "close_app", "media", "open_app",
        "open_url", "power", "quit_app", "switch_to_app", "volume", "web_search",
    }


def test_confirm_policy(monkeypatch):
    tools = discover_tools()
    monkeypatch.setattr(apps, "_close_plan", lambda name: ("windows", object()))
    assert tools["close_app"].requires_confirm({"name": "微信"})
    assert tools["quit_app"].requires_confirm({"name": "豆包"})
    assert tools["power"].requires_confirm({"action": "shutdown"})
    assert tools["power"].requires_confirm({"action": "restart"})
    for action in ("lock", "sleep", "cancel"):
        assert not tools["power"].requires_confirm({"action": action})
    for name in ("open_app", "switch_to_app", "volume", "brightness", "media", "web_search", "clock"):
        assert not tools[name].requires_confirm({}), name


# ── 软件名匹配：用一份假的应用清单 ──

FAKE_APPS = [apps.App(n, n) for n in [
    "微信", "企业微信", "记事本", "计算器", "Google Chrome", "网易云音乐",
    "网易有道翻译", "QQ音乐", "QQ邮箱", "QQ", "Microsoft Edge", "Visual Studio Code",
]]


@pytest.fixture
def fake_index(monkeypatch):
    monkeypatch.setattr(apps, "installed_apps", lambda refresh=False: FAKE_APPS)


@pytest.mark.parametrize("said, expected", [
    ("微信", "微信"),
    ("危信", "微信"),               # 同音字，靠拼音
    ("weixin", "微信"),
    ("记事本", "记事本"),
    ("notepad", "记事本"),          # 别名
    ("chrome", "Google Chrome"),
    ("CROME", "Google Chrome"),      # 识别把 Chrome 听错了
    ("网易云", "网易云音乐"),        # 配置里的别名 + 包含匹配
    ("QQ", "QQ"),
    ("浏览器", "Microsoft Edge"),
    ("vscode", "Visual Studio Code"),
])
def test_find_app(fake_index, said, expected):
    app, candidates = apps.find_app(said)
    assert app is not None, f"{said} → 候选 {[c.name for c in candidates]}"
    assert app.name == expected


def test_find_app_ambiguous(fake_index):
    app, candidates = apps.find_app("网易")
    assert app is None
    assert {"网易云音乐", "网易有道翻译"} <= {c.name for c in candidates}


def test_find_app_missing(fake_index):
    assert apps.find_app("鸿蒙模拟器") == (None, [])


def test_real_start_menu_has_apps():
    found = apps.installed_apps(refresh=True)
    assert len(found) > 10
    assert all(a.target for a in found)


# ── 电源：只测命令拼得对不对，绝不真关机 ──

def test_shutdown_command(monkeypatch):
    sent = []
    monkeypatch.setattr(power, "_run", lambda args: sent.append(args) or 0)
    tool = power.PowerTool()
    assert "60 秒后关机" in run(tool.execute("shutdown"))
    assert sent[0][:3] == ["/s", "/t", "60"]
    run(tool.execute("cancel"))
    assert sent[1] == ["/a"]


def test_cancel_when_nothing_scheduled(monkeypatch):
    monkeypatch.setattr(power, "_run", lambda args: power.NOTHING_TO_ABORT)
    assert run(power.PowerTool().execute("cancel")) == "现在没有安排关机或重启"


# ── 音量：真实读写，测完恢复 ──

@pytest.mark.system
def test_volume_roundtrip():
    original, muted = volume.get_volume()
    target = original + 1 if original < 100 else original - 1
    try:
        out = run(volume.VolumeTool().execute("set", target))
        assert out == f"音量已调到 {target}%"
        assert volume.get_volume()[0] == target
    finally:
        volume.set_volume(original)
        volume.set_mute(muted)
    assert volume.get_volume() == (original, muted)


# ── 正在运行的程序：用假窗口测"按程序找"、托盘里的软件、关闭前要不要问 ──

from agent.tools._win32 import Window  # noqa: E402

FAKE_MENU = [
    apps.App("豆包", r"shell:AppsFolder\Doubao.ChatApp"),
    apps.App("记事本", r"shell:AppsFolder\Microsoft.WindowsNotepad_8wekyb3d8bbwe!App"),
    apps.App("微信", r"shell:AppsFolder\{6D809377-6AF0-444B-8957-A3773F02200E}\Tencent\Weixin\Weixin.exe"),
]
NOTEPAD_EXE = r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe"
FAKE_WINDOWS = [
    # 豆包：主窗口收进了托盘，看得见的只有没标题的悬浮球
    Window(1, "豆包", 10, "Doubao.exe", r"C:\x\Doubao.exe", visible=False, main=True),
    Window(2, "", 10, "Doubao.exe", r"C:\x\Doubao.exe", visible=True, main=False),
    # 英文界面的记事本：标题里没有"记事本"三个字
    Window(3, "assistant.log - Notepad", 20, "Notepad.exe", NOTEPAD_EXE, visible=True, main=True),
    Window(4, "微信", 30, "Weixin.exe", r"C:\Program Files\Tencent\Weixin\Weixin.exe", visible=True, main=True),
]


@pytest.fixture
def fake_running(monkeypatch):
    monkeypatch.setattr(apps, "installed_apps", lambda refresh=False: FAKE_MENU)
    monkeypatch.setattr(apps._win32, "list_windows", lambda: FAKE_WINDOWS)
    monkeypatch.setattr(apps._win32, "file_description", lambda exe: "")
    monkeypatch.setattr(apps, "find_process", lambda name: (None, []))   # 不去看真实的进程


@pytest.mark.parametrize("said, label, shown, tucked", [
    ("豆包", "豆包", 0, 1),
    ("记事本", "记事本", 1, 0),          # 靠开始菜单的名字对上英文界面的记事本
    ("微信", "微信", 1, 0),
])
def test_find_running(fake_running, said, label, shown, tucked):
    app, _ = apps.find_running(said)
    assert app is not None and app.label == label
    assert (len(app.shown), len(app.tucked)) == (shown, tucked)


def test_close_tray_app_goes_to_quit(fake_running, monkeypatch):
    """豆包只有收进托盘的窗口：不关窗口（关了也没用），改走"彻底退出"。"""
    group = fake_group()
    monkeypatch.setattr(apps, "find_process", lambda name: (group, []))
    closed = []
    monkeypatch.setattr(apps._win32, "close_window", lambda hwnd: closed.append(hwnd))
    assert apps._close_plan("豆包") == ("quit", group)
    assert not closed


def test_close_not_running(fake_running):
    tool = apps.CloseAppTool()
    assert not tool.requires_confirm({"name": "网易云音乐"})
    assert "没有在运行" in run(tool.execute("网易云音乐"))


def test_close_open_app_asks_and_closes(fake_running, monkeypatch):
    closed = []
    monkeypatch.setattr(apps._win32, "close_window", lambda hwnd: closed.append(hwnd))
    monkeypatch.setattr(apps, "_process_alive", lambda process: False)
    real_sleep = asyncio.sleep                      # 先存下来：patch 的是全局的 asyncio.sleep
    monkeypatch.setattr(apps.asyncio, "sleep", lambda s: real_sleep(0))
    tool = apps.CloseAppTool()
    assert tool.requires_confirm({"name": "记事本"})
    assert run(tool.execute("记事本")) == "已关闭记事本"
    assert closed == [3]



# ── 彻底退出：用假进程测，绝不结束真的程序 ──

class FakeProc:
    def __init__(self, pid):
        self.pid, self.terminated = pid, False

    def terminate(self):
        self.terminated = True


def fake_group(label="豆包", protected=False, n=3):
    return apps.ProcessGroup(label, "Doubao.exe", [FakeProc(i) for i in range(n)], protected, ["doubao"])


@pytest.fixture
def no_real_windows(monkeypatch):
    monkeypatch.setattr(apps._win32, "list_windows", lambda: [])
    # 第一次等待：都还活着（托盘软件收到"关闭"不会退出）；强制结束后再等：都没了
    monkeypatch.setattr(apps.psutil, "wait_procs",
                        lambda procs, timeout: (([], list(procs)) if not any(p.terminated for p in procs)
                                                else (list(procs), [])))


def test_quit_group_terminates_after_grace(no_real_windows):
    group = fake_group()
    assert apps.quit_group(group) == "已彻底退出豆包"
    assert all(p.terminated for p in group.procs)


def test_quit_group_refuses_system_programs(no_real_windows):
    group = fake_group("Windows 资源管理器", protected=True)
    out = apps.quit_group(group)
    assert out.is_error and "系统程序" in out
    assert not any(p.terminated for p in group.procs)


def test_close_background_app_asks_then_quits(monkeypatch):
    group = fake_group()
    monkeypatch.setattr(apps, "find_running", lambda name: (None, []))
    monkeypatch.setattr(apps, "find_process", lambda name: (group, []))
    quit_calls = []
    monkeypatch.setattr(apps, "quit_group", lambda g: quit_calls.append(g) or "已彻底退出豆包")
    tool = apps.CloseAppTool()
    assert tool.requires_confirm({"name": "豆包"})
    assert tool.confirm_prompt({"name": "豆包"}) == "豆包在后台运行，要彻底退出它吗？"
    assert run(tool.execute("豆包")) == "已彻底退出豆包"
    assert quit_calls == [group]


def test_close_protected_does_not_ask(monkeypatch):
    monkeypatch.setattr(apps, "find_running", lambda name: (None, []))
    monkeypatch.setattr(apps, "find_process", lambda name: (fake_group("Windows 资源管理器", protected=True), []))
    tool = apps.CloseAppTool()
    assert not tool.requires_confirm({"name": "资源管理器"})
    assert "系统程序" in run(tool.execute("资源管理器"))
