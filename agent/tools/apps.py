"""软件相关的三个工具：open_app / switch_to_app / close_app。

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

import json
import os
import re
import subprocess
import time
from dataclasses import dataclass

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


def find_windows(name: str) -> tuple[list[_win32.Window], list[str]]:
    """找打开着的窗口：返回 (最像的那个程序的所有窗口, 候选描述)。"""
    name = resolve_alias(name)
    windows = [w for w in _win32.list_windows() if w.title != "Program Manager"]
    items = [
        (w, keys_for(w.title) + keys_for(w.process.removesuffix(".exe")))
        for w in windows
    ]
    best, candidates = pick(rank(name, items))
    if best is None:
        return [], [f"{w.title}（{w.process}）" for w in candidates]
    same_process = [w for w in windows if w.process == best.process and best.process]
    return same_process or [best], []


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
        windows, candidates = find_windows(name)
        if not windows:
            hint = f"差不多的有：{'、'.join(candidates)}" if candidates else "可以用 open_app 打开它"
            return ToolResult.error(f"没有找到开着的「{name}」窗口。{hint}")
        target = windows[0]
        if not _win32.bring_to_front(target.hwnd):
            return ToolResult.error(f"Windows 不让切换到「{target.title}」，可能是它以管理员身份运行")
        return f"已切换到 {target.title}"


class CloseAppTool(Tool):
    name = "close_app"
    description = (
        "关闭一个软件的所有窗口，效果等于点窗口右上角的 ×（软件有没保存的内容会自己弹窗问）。"
        "不会强制结束进程。"
    )
    parameters = {
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "软件名或窗口标题里的字，如 微信、记事本"},
        },
        "required": ["name"],
    }
    needs_confirm = True

    def confirm_prompt(self, args: dict) -> str:
        return f"确定要关闭{args.get('name', '这个软件')}吗？"

    async def execute(self, name: str) -> str:
        windows, candidates = find_windows(name)
        if not windows:
            hint = f"差不多的有：{'、'.join(candidates)}" if candidates else ""
            return ToolResult.error(f"没有找到开着的「{name}」窗口。{hint}")
        failures = [err for w in windows if (err := _win32.close_window(w.hwnd))]
        if failures:
            return ToolResult.error(failures[0])
        return f"已关闭 {windows[0].process.removesuffix('.exe') or windows[0].title}（{len(windows)} 个窗口）"
