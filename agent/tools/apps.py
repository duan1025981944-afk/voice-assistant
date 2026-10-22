"""软件相关的三个工具：open_app / switch_to_app / close_app。

──────────────────────────────────────────────────────────────
"关掉豆包"是怎么找到豆包的
──────────────────────────────────────────────────────────────
按**程序**找，不按单个窗口找。同一个程序的窗口归成一组（RunningApp），
这一组的名字来自三处：进程名（Doubao）、程序文件里写的产品名（微信）、主窗口标题。
窗口分两种：看得见的主窗口，和点了 × 后收进托盘、藏起来的主窗口。
只看"看得见、有标题"的窗口会把收进托盘的软件误判成"没开"——豆包就是这样：
主窗口藏着，看得见的只有一个没标题的悬浮球。

──────────────────────────────────────────────────────────────
"打开微信"是怎么找到微信的
──────────────────────────────────────────────────────────────
1. 应用清单来自 PowerShell 的 Get-StartApps：开始菜单里能看到的软件都在
   （桌面软件和商店应用都有），每个都有一个 AppID，用
   os.startfile("shell:AppsFolder\\<AppID>") 就能启动，不需要知道 exe 在哪。
   再补几个开始菜单里没有的系统程序（任务管理器、控制面板……）。
2. 匹配：语音识别可能给出同音字（"危信"），所以名字和拼音各比一次，取高分。
3. 只有一个明显最像的才直接打开；几个差不多像的，把候选交给模型，让它问用户。

清单要跑一次 PowerShell（约 1 秒），所以缓存 10 分钟；找不到时会强制刷新一次，
刚装的软件也能找到。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

import psutil
from pypinyin import lazy_pinyin
from rapidfuzz import fuzz

from agent.tools import _win32
from agent.tools.base import Tool, ToolResult
from config.loader import load_voice_settings

# 能直接打开的阈值、"差不多像"的范围、能当候选的最低分
OPEN_SCORE = 75
TIE_MARGIN = 8
CANDIDATE_SCORE = 55

CACHE_SECONDS = 600

# 开始菜单里没有、但常被叫到的系统程序：名字 → os.startfile 的目标
BUILTIN_APPS = {
    "任务管理器": "taskmgr.exe",
    "控制面板": "control.exe",
    "设置": "ms-settings:",
    "画图": "mspaint.exe",
    "文件资源管理器": "explorer.exe",
}

# 常见的别称 → 正式名。config.json 的 voice.appAliases 会覆盖、补充这里
DEFAULT_ALIASES = {
    "浏览器": "Microsoft Edge",
    "edge": "Microsoft Edge",
    "资源管理器": "文件资源管理器",
    "文件管理器": "文件资源管理器",
    "此电脑": "文件资源管理器",
    "我的电脑": "文件资源管理器",
    "notepad": "记事本",
    "calculator": "计算器",
    "vscode": "Visual Studio Code",
}


@dataclass(frozen=True)
class App:
    name: str
    target: str        # 交给 os.startfile 的东西


def normalize(text: str) -> str:
    """小写、去掉空格和标点，"Google Chrome" → "googlechrome"。"""
    return re.sub(r"[\s\-_·.()（）]+", "", text.lower())


def keys_for(text: str) -> list[str]:
    """一个名字的几种比较形态：原文、拼音。

    >>> keys_for("QQ音乐")
    ['qq音乐', 'qqyinyue']
    """
    plain = normalize(text)
    pinyin = normalize("".join(lazy_pinyin(text)))
    return [plain] if pinyin == plain else [plain, pinyin]


def score(query: str, key: str) -> float:
    """0–100，越大越像。完全相同 100；一个包含另一个 70–95；其余按编辑距离，最多 90。

    4 个字符以上的查询再看一下"最像的那一截"（partial_ratio），
    识别把 Chrome 听成 CROME 时也能对上 Google Chrome。

    >>> score("记事本", "记事本")
    100
    >>> score("网易云", "网易云音乐") > 85
    True
    >>> score("crome", "googlechrome") >= 75
    True
    """
    if not query or not key:
        return 0
    if query == key:
        return 100
    if query in key:
        return 80 + 15 * len(query) / len(key)
    if len(key) >= 2 and key in query and len(key) / len(query) >= 0.5:
        return 70 + 20 * len(key) / len(query)
    similar = fuzz.ratio(query, key) * 0.9
    if len(query) >= 4:
        similar = max(similar, fuzz.partial_ratio(query, key) * 0.85)
    return similar


def rank(query: str, items: list[tuple[object, list[str]]]) -> list[tuple[object, float]]:
    """把 items 按和 query 的相似度从高到低排好。items 是 (东西, 它的比较形态列表)。"""
    q_keys = keys_for(query)
    scored = [
        (obj, max(score(q, k) for q in q_keys for k in keys))
        for obj, keys in items
    ]
    return sorted(scored, key=lambda pair: pair[1], reverse=True)


def pick(ranked: list[tuple[object, float]]) -> tuple[object | None, list[object]]:
    """从排好的结果里挑：(确定的那一个, 候选列表)。两者最多有一个不是空的。

    >>> pick([("微信", 100), ("企业微信", 88)])
    ('微信', [])
    >>> pick([("QQ音乐", 87), ("QQ邮箱", 84)])
    (None, ['QQ音乐', 'QQ邮箱'])
    """
    if not ranked or ranked[0][1] < CANDIDATE_SCORE:
        return None, []
    best, best_score = ranked[0]
    runner_up = ranked[1][1] if len(ranked) > 1 else 0
    clear_winner = (best_score >= 99 and runner_up < 99) or best_score - runner_up >= TIE_MARGIN
    if best_score >= OPEN_SCORE and clear_winner:
        return best, []
    return None, [obj for obj, s in ranked[:3] if s >= CANDIDATE_SCORE]


# ── 应用清单 ──────────────────────────────────────────────

_cache: tuple[float, list[App]] | None = None


def _start_apps() -> list[App]:
    """跑 Get-StartApps。PWA（Chrome 装的网页应用）和正式软件重名时，留正式软件。"""
    cmd = (
        "[Console]::OutputEncoding=[Text.Encoding]::UTF8;"
        "Get-StartApps | ConvertTo-Json -Compress"
    )
    out = subprocess.run(
        ["powershell", "-NoProfile", "-NonInteractive", "-Command", cmd],
        capture_output=True, timeout=20, creationflags=subprocess.CREATE_NO_WINDOW,
    ).stdout.decode("utf-8", errors="replace")
    rows = json.loads(out or "[]")
    if isinstance(rows, dict):               # 只有一项时 ConvertTo-Json 不给数组
        rows = [rows]

    by_name: dict[str, App] = {}
    for row in rows:
        name, app_id = row.get("Name", ""), row.get("AppID", "")
        if not name or not app_id or re.search(r"卸载|uninstall", name, re.I):
            continue
        is_pwa = app_id.startswith("Chrome._crx_") or app_id.startswith("MSEdge._crx_")
        if name in by_name and is_pwa:
            continue
        by_name[name] = App(name, f"shell:AppsFolder\\{app_id}")
    return list(by_name.values())


def installed_apps(*, refresh: bool = False) -> list[App]:
    global _cache
    if refresh or _cache is None or time.monotonic() - _cache[0] > CACHE_SECONDS:
        apps = _start_apps()
        names = {a.name for a in apps}
        apps += [App(n, t) for n, t in BUILTIN_APPS.items() if n not in names]
        _cache = (time.monotonic(), apps)
    return _cache[1]


def resolve_alias(name: str) -> str:
    aliases = {**DEFAULT_ALIASES, **load_voice_settings().app_aliases}
    lowered = {normalize(k): v for k, v in aliases.items()}
    return lowered.get(normalize(name), name)


def find_app(name: str) -> tuple[App | None, list[App]]:
    name = resolve_alias(name)
    for refresh in (False, True):
        apps = installed_apps(refresh=refresh)
        app, candidates = pick(rank(name, [(a, keys_for(a.name)) for a in apps]))
        if app or candidates:
            return app, candidates
    return None, []


# 不当成"软件"的进程：任务栏和开始菜单的零件、输入法、我们自己
IGNORED_PROCESSES = {"textinputhost.exe", "shellexperiencehost.exe", "searchhost.exe",
                     "startmenuexperiencehost.exe", "dwm.exe", "python.exe", "pythonw.exe"}
IGNORED_TITLES = {"Program Manager"}
# 这个进程替所有商店应用托管窗口（计算器、设置……），只能按窗口一个个算
UWP_HOST = "applicationframehost.exe"


@dataclass
class RunningApp:
    label: str                      # 给人看的名字
    windows: list[_win32.Window]

    @property
    def shown(self) -> list[_win32.Window]:
        return [w for w in self.windows if w.shown]

    @property
    def tucked(self) -> list[_win32.Window]:
        return [w for w in self.windows if w.tucked]

    @property
    def process(self) -> str:
        return self.windows[0].process

    def keys(self) -> list[str]:
        w = self.windows[0]
        texts = {w.process.removesuffix(".exe"), _win32.file_description(w.exe), self.label}
        texts |= {x.title for x in self.windows if x.main and x.title}
        texts |= set(start_menu_names(w.exe, w.process))
        return [k for t in texts if t for k in keys_for(t)]


def _package_family(exe: str) -> str:
    r"""商店应用的包名：WindowsApps\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\…
    → Microsoft.WindowsNotepad_8wekyb3d8bbwe（开始菜单里的 AppID 就以它开头）。

    >>> _package_family(r"C:\Program Files\WindowsApps\Microsoft.WindowsNotepad_11.2607.14.0_x64__8wekyb3d8bbwe\Notepad\Notepad.exe")
    'Microsoft.WindowsNotepad_8wekyb3d8bbwe'
    >>> _package_family(r"C:\Program Files\Tencent\Weixin\Weixin.exe")
    ''
    """
    m = re.search(r"\\WindowsApps\\([^\\_]+)_[^\\_]+_[^\\_]*_[^\\_]*_([^\\_]+)\\", exe)
    return f"{m.group(1)}_{m.group(2)}" if m else ""


def start_menu_names(exe: str, process: str) -> list[str]:
    """这个程序在开始菜单里叫什么，"打开"和"关闭"用同一套名字才不会对不上。

    三种对法（AppID 来自 Get-StartApps）：
        商店应用    AppID 以包名开头：Microsoft.WindowsNotepad_8wekyb3d8bbwe!App → 记事本
        普通程序    AppID 是 exe 路径：…\\Tencent\\Weixin\\Weixin.exe → 微信
        自定义 ID   AppID 第一段和进程名相同：Doubao.ChatApp ↔ Doubao.exe → 豆包
    """
    family = _package_family(exe).lower()
    stem = process.lower().removesuffix(".exe")
    names = []
    for app in installed_apps():
        app_id = app.target.removeprefix("shell:AppsFolder\\").lower()
        if app_id == app.target.lower():
            continue                              # 内置的系统程序，没有 AppID
        if (family and app_id.startswith(family + "!")) \
                or (process and app_id.endswith("\\" + process.lower())) \
                or (stem and "\\" not in app_id and "!" not in app_id and app_id.split(".")[0] == stem):
            names.append(app.name)
    return names


def running_apps(windows: list[_win32.Window] | None = None) -> list[RunningApp]:
    """把所有窗口按程序归组。"""
    windows = _win32.list_windows() if windows is None else windows
    groups: dict[str, list[_win32.Window]] = {}
    for w in windows:
        proc = w.process.lower()
        if not proc or proc in IGNORED_PROCESSES or w.title in IGNORED_TITLES:
            continue
        key = f"{proc}|{w.hwnd}" if proc == UWP_HOST else proc
        groups.setdefault(key, []).append(w)

    return [RunningApp(label=_label(ws), windows=ws) for ws in groups.values()]


def _label(windows: list[_win32.Window]) -> str:
    """念给用户听的名字。优先级：
    1. 程序文件里写的中文产品名（"Windows 资源管理器"）——最稳，不随窗口内容变
    2. 开始菜单里的名字（"记事本""微信"）——和"打开"时用的名字一致
    3. 短的主窗口标题（"豆包"）
    4. 英文产品名、长标题、进程名
    """
    first = windows[0]
    description = _win32.file_description(first.exe)
    if re.search(r"[一-鿿]", description):
        return description
    menu = start_menu_names(first.exe, first.process)
    if menu:
        return menu[0]
    titles = [w.title for w in windows if w.shown and w.title] + [w.title for w in windows if w.tucked]
    short = next((t for t in titles if len(t) <= 8), "")
    return short or description or (titles[0] if titles else "") or first.process.removesuffix(".exe")


def find_running(name: str, apps: list[RunningApp] | None = None) -> tuple[RunningApp | None, list[RunningApp]]:
    """按名字找正在运行的程序：(确定的那个, 候选)。

    原话和别名各比一次取高分：别名是给"打开"用的（"资源管理器"→开始菜单里的"文件资源管理器"），
    正在运行的程序不一定叫那个名字（资源管理器进程写的是"Windows 资源管理器"）。
    """
    apps = running_apps() if apps is None else apps
    items = [(a, a.keys()) for a in apps]
    best: dict[int, tuple[RunningApp, float]] = {}
    for query in {name, resolve_alias(name)}:
        for app, s in rank(query, items):
            if id(app) not in best or s > best[id(app)][1]:
                best[id(app)] = (app, s)
    return pick(sorted(best.values(), key=lambda pair: pair[1], reverse=True))


def _process_alive(process: str) -> bool:
    return any(p.info["name"] == process for p in psutil.process_iter(["name"]))


# ── 工具 ──────────────────────────────────────────────────


class OpenAppTool(Tool):
    name = "open_app"
    description = (
        "打开（启动）电脑上安装的软件，如微信、记事本、Chrome、网易云音乐。"
        "软件已经开着时也可以用它，通常会把它带到前面。"
        "不能打开网址（用 open_url）或文件。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "软件名，按用户的说法填，如 微信、记事本、chrome"},
        },
        "required": ["name"],
    }
    needs_confirm = False

    async def execute(self, name: str) -> str:
        app, candidates = find_app(name)
        if app is None and candidates:
            names = "、".join(c.name for c in candidates)
            return ToolResult.error(f"有几个软件都像「{name}」：{names}。问用户要哪一个")
        if app is None:
            return ToolResult.error(f"开始菜单里没有找到「{name}」这个软件")
        try:
            os.startfile(app.target)
        except OSError as exc:
            return ToolResult.error(f"打开 {app.name} 失败：{exc}")
        return f"已打开 {app.name}"


class SwitchToAppTool(Tool):
    name = "switch_to_app"
    description = (
        "把一个已经打开的软件窗口切到最前面。"
        "软件没开着时会失败，这时改用 open_app。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "软件名或窗口标题里的字，如 微信、chrome、记事本"},
        },
        "required": ["name"],
    }
    needs_confirm = False

    async def execute(self, name: str) -> str:
        app, candidates = find_running(name)
        if app is None:
            hint = f"差不多的有：{'、'.join(c.label for c in candidates)}" if candidates else "可以用 open_app 打开它"
            return ToolResult.error(f"「{name}」没有在运行。{hint}")
        if app.shown:
            ok = _win32.bring_to_front(app.shown[0].hwnd)
        elif app.tucked:
            ok = _win32.show_window(app.tucked[0].hwnd)    # 收进托盘了：把主窗口重新显示出来
        else:
            return ToolResult.error(f"{app.label}在后台运行，但没有能显示的窗口，改用 open_app 试试")
        if not ok:
            return ToolResult.error(f"Windows 不让切换到{app.label}，可能是它以管理员身份运行")
        return f"已切换到 {app.label}"


class CloseAppTool(Tool):
    name = "close_app"
    description = (
        "关闭一个软件的所有窗口，效果等于点窗口右上角的 ×（软件有没保存的内容会自己弹窗问）。"
        "不会强制结束进程：有些软件关掉窗口后还在托盘里运行，工具会如实返回，照实告诉用户。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "软件名或窗口标题里的字，如 微信、记事本"},
        },
        "required": ["name"],
    }
    def requires_confirm(self, args: dict) -> bool:
        # 只有真有窗口要关时才问；没在运行、或者只在托盘里，什么都不会发生，不用问
        app, _ = find_running(args.get("name", ""))
        return app is not None and bool(app.shown)

    def confirm_prompt(self, args: dict) -> str:
        return f"确定要关闭{args.get('name', '这个软件')}吗？"

    async def execute(self, name: str) -> str:
        app, candidates = find_running(name)
        if app is None:
            hint = f"差不多的有：{'、'.join(c.label for c in candidates)}。" if candidates else ""
            return f"「{name}」没有在运行，不用关。{hint}"
        if not app.shown:
            return (f"{app.label}没有打开的窗口，它在后台运行（托盘图标或悬浮窗）。"
                    "关窗口我能做，让它完全退出我做不到：要退出得在它的托盘图标上右键选退出。")
        failures = [err for w in app.shown if (err := _win32.close_window(w.hwnd))]
        if failures:
            return ToolResult.error(failures[0])
        await asyncio.sleep(1.0)                   # 给它一点时间退出，再看进程还在不在
        if _process_alive(app.process):
            return f"已关闭{app.label}的窗口，不过它还在后台运行（比如托盘里），要完全退出得在托盘图标上选退出"
        return f"已关闭{app.label}"
