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
        "open_url", "power", "switch_to_app", "volume", "web_search",
    }


def test_confirm_policy():
    tools = discover_tools()
    assert tools["close_app"].requires_confirm({"name": "微信"})
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
