"""编排层：把耳朵、大脑、嘴串成一个状态机。

──────────────────────────────────────────────────────────────
状态
──────────────────────────────────────────────────────────────
    IDLE ──唤醒词/快捷键──▶ LISTENING ──说完──▶ THINKING ──▶ SPEAKING ──念完──▶ IDLE
      ▲                        │                                │
      └──────── 没开口 ────────┘          唤醒词/快捷键：打断，直接 LISTENING
    PAUSED：托盘里点了"暂停监听"，麦克风流整个关掉

──────────────────────────────────────────────────────────────
线程
──────────────────────────────────────────────────────────────
    耳朵线程   不停地从麦克风取音频：待命时喂唤醒词检测，在听时喂断句，说完就识别
    大脑线程   一个 asyncio 事件循环，跑 runner.run_turn 和朗读
    快捷键线程 Windows 消息循环，按下快捷键时通知耳朵线程
    主线程     托盘图标（pystray 要求在主线程）

──────────────────────────────────────────────────────────────
"代数"（_gen）是干什么的
──────────────────────────────────────────────────────────────
用户在助手说话时喊唤醒词，会开始新的一轮。上一轮的大脑协程这时还在收尾
（朗读刚被打断返回），它接下来那句"状态改回 IDLE"不能生效，否则会把
新一轮的 LISTENING 冲掉。所以每次开始新一轮 _gen 加一，大脑改状态时带上
自己那一轮的代数，对不上就不改。
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import enum
import json
import logging
import threading
import time
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
from typing import Callable

from agent.brain import build_runner
from agent.tools.base import Tool
from config.loader import load_pricing
from config.schema import Pricing, VoiceSettings
from voice.hotkey import Hotkey
from voice.listen import SpeechCollector, Transcriber
from voice.mic import Microphone, MicrophoneError
from voice.paths import DATA_DIR
from voice.tts import Speaker, SpeechError, chime
from voice.wake import WakeWordDetector

log = logging.getLogger("assistant")

# 确认时的回答怎么算。会问确认的只有"关软件 / 退出软件 / 关机 / 重启"，
# 所以"关""退出""重启"本身就算同意（"不退出"会先被否定词拦下）。
YES = ("确认", "确定", "同意", "是", "对", "好", "行", "可以", "嗯", "没问题", "没错", "当然",
       "执行", "要", "关", "退出", "重启", "yes", "yeah", "ok", "sure")
NO = ("不", "别", "取消", "算了", "停", "等等", "no")


class State(enum.Enum):
    IDLE = "待命"
    LISTENING = "在听"
    THINKING = "在想"
    SPEAKING = "在说"
    PAUSED = "已暂停"


def classify_answer(answer: str | None) -> bool | None:
    """True 同意 / False 不同意 / None 没听清（没说话，或者听不出是哪个意思）。

    先看否定词（"不确定""不要关"里也有肯定的字），再看肯定词。

    >>> [classify_answer(a) for a in ["确认", "关闭吧", "关掉", "YES", "对对对", "好的关吧", "退出吧"]]
    [True, True, True, True, True, True, True]
    >>> [classify_answer(a) for a in ["不确定", "不要关", "取消", "算了吧", "No.", "先不退出"]]
    [False, False, False, False, False, False]
    >>> [classify_answer(a) for a in ["啊？", "什么", "", None]]
    [None, None, None, None]
    """
    if not answer:
        return None
    text = answer.lower()
    if any(w in text for w in NO):
        return False
    if any(w in text for w in YES):
        return True
    return None


@dataclass
class Progress:
    """做到哪一步了，给右下角的进度气泡看。start=新一轮开始（计时归零），done=这一轮结束。"""

    text: str
    start: bool = False
    done: bool = False


# 调工具时气泡上显示的话；{name} 这类占位符用工具参数填
TOOL_PROGRESS = {
    "open_app": "正在打开 {name}",
    "switch_to_app": "正在切换到 {name}",
    "close_app": "正在关闭 {name}",
    "quit_app": "正在彻底退出 {name}",
    "volume": "正在调音量",
    "brightness": "正在调亮度",
    "media": "正在控制播放",
    "web_search": "正在搜索 {query}",
    "open_url": "正在打开 {url}",
    "power": "正在执行电源操作",
    "clock": "正在看时间",
}


def tool_progress(name: str, args: dict) -> str:
    """调工具时气泡上那句话。

    >>> tool_progress("open_app", {"name": "微信"})
    '正在打开 微信'
    >>> tool_progress("open_app", {})
    '正在打开 '
    >>> tool_progress("new_tool", {})
    '正在执行 new_tool'
    """
    return TOOL_PROGRESS.get(name, f"正在执行 {name}").format_map(defaultdict(str, args))


def format_tokens(n: int) -> str:
    """token 数写成好读的样子。

    >>> [format_tokens(n) for n in (0, 2345, 123456)]
    ['0', '2,345', '12.3 万']
    """
    return f"{n / 10000:.1f} 万" if n >= 100000 else f"{n:,}"


def format_cost(cost: float, pricing: Pricing) -> str:
    """花费写成好读的样子；没配价格返回空串。

    >>> p = Pricing(cache_miss=1)
    >>> [format_cost(c, p) for c in (0, 0.00213, 0.5)]
    ['0 元', '0.0021 元', '0.50 元']
    >>> format_cost(0.1, Pricing())      # 没配价格
    ''
    """
    if not pricing.known:
        return ""
    if cost == 0:
        return f"0 {pricing.currency}"
    return f"{cost:.4f} {pricing.currency}" if cost < 0.01 else f"{cost:.2f} {pricing.currency}"


def _warm_up() -> None:
    try:
        from agent.tools import apps
        apps.process_groups()
    except Exception:
        log.exception("预热软件列表失败（不影响使用，第一次用时会慢一点）")


def _resolve(future: concurrent.futures.Future, value) -> None:
    """给 future 填结果；已经填过（被别的线程抢先取消了）就算了。"""
    try:
        future.set_result(value)
    except concurrent.futures.InvalidStateError:
        pass


class EventLog:
    """每一轮记一行 JSON：听到什么、各环节花了多久、调了哪些工具、回了什么。只记文字，不存录音。"""

    def __init__(self):
        self.dir = DATA_DIR / "events"
        self.dir.mkdir(parents=True, exist_ok=True)

    def write(self, **fields) -> None:
        fields = {"time": datetime.now().isoformat(timespec="seconds"), **fields}
        path = self.dir / f"{datetime.now():%Y%m%d}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(fields, ensure_ascii=False) + "\n")

    def totals(self, day: date | None = None) -> dict:
        """某一天（不传就是全部）一共几轮、多少 token、多少钱。早期的记录没有 token 字段，按 0 算。"""
        pattern = f"{day:%Y%m%d}.jsonl" if day else "*.jsonl"
        total = {"turns": 0, "tokens": 0, "cost": 0.0, "since": ""}
        for path in sorted(self.dir.glob(pattern)):
            with open(path, encoding="utf-8") as f:
                for line in f:
                    if not line.strip():
                        continue
                    event = json.loads(line)
                    total["turns"] += 1
                    total["tokens"] += event.get("prompt_tokens", 0) + event.get("completion_tokens", 0)
                    total["cost"] += event.get("cost", 0.0)
                    if not total["since"] and "prompt_tokens" in event:
                        total["since"] = event["time"][:10]       # 从哪天开始有 token 记录
        return total


class Assistant:
    def __init__(self, settings: VoiceSettings):
        self.settings = settings
        log.info("加载模型…")
        self.mic = Microphone(settings.input_device)
        self.wake = WakeWordDetector(settings.wake_words, settings.wake_threshold, settings.wake_score)
        self.collector = SpeechCollector(settings.end_silence, settings.max_speech)
        self.transcribe = Transcriber()
        self.speaker = Speaker(settings.tts_voice, settings.tts_rate, settings.tts_pitch)
        self.runner = build_runner(self._confirm, settings)
        self.runner.on_tool = lambda name, args: self._progress(tool_progress(name, args))
        self.events = EventLog()
        self.pricing = load_pricing()

        self.state = State.IDLE
        self.on_state: Callable[[State], None] = lambda s: None         # 托盘会换成自己的
        self.on_progress: Callable[[Progress], None] = lambda p: None   # 进度气泡会换成自己的
        self._lock = threading.RLock()
        self._gen = 0
        self._answer: concurrent.futures.Future | None = None       # 正在等"确认吗"的回答
        self._trigger = ""
        self._listen_started = 0.0
        self._hotkey_pressed = threading.Event()
        self._quit = threading.Event()

        self.loop = asyncio.new_event_loop()
        # 同一时间只跑一轮 run_turn。正常情况下"在想"时不接新指令，用不上它；
        # 但用户在"确定要关机吗"念到一半时喊唤醒词、马上说了新指令，
        # 上一轮还在收尾，两轮同时改 runner.messages 会把上下文搅乱。
        self._turn_lock = asyncio.Lock()
        self._brain = threading.Thread(target=self.loop.run_forever, name="brain", daemon=True)
        self._ears = threading.Thread(target=self._ear_loop, name="ears", daemon=True)
        self.hotkey = Hotkey(settings.hotkey, self._hotkey_pressed.set)

    # ── 生命周期 ──────────────────────────────────────────

    def start(self) -> None:
        self.mic.start()
        self._brain.start()
        self._ears.start()
        self.hotkey.start()
        self.hotkey.ready.wait(2)
        if self.hotkey.error:
            log.warning(self.hotkey.error)
        # 后台先把开始菜单和程序信息读一遍（第一次要 1 秒左右），第一次说"打开/关掉 X"时就不用等
        threading.Thread(target=_warm_up, name="warm-up", daemon=True).start()
        words = "、".join(self.settings.wake_words)
        log.info("就绪。说「%s」或按 %s 开始", words, self.settings.hotkey)

    def shutdown(self) -> None:
        self._quit.set()
        self.speaker.stop()
        self.hotkey.stop()
        self.mic.stop()
        self.loop.call_soon_threadsafe(self.loop.stop)

    def pause(self) -> None:
        with self._lock:
            self._gen += 1
            self._cancel_answer()
            self.speaker.stop()
            self.mic.stop()
            self._set_state(State.PAUSED)

    def resume(self) -> None:
        with self._lock:
            self.mic.start()
            self.wake.reset()
            self._set_state(State.IDLE)

    # ── 状态 ──────────────────────────────────────────────

    def _set_state(self, state: State, gen: int | None = None) -> None:
        with self._lock:
            if gen is not None and gen != self._gen:
                return                  # 过期的一轮，不许改
            if self.state is State.PAUSED and state is not State.IDLE and gen is not None:
                return
            if state is not self.state:
                log.debug("%s → %s", self.state.value, state.value)
                self.state = state
                self.on_state(state)

    def _begin_listening(self, trigger: str, *, new_turn: bool = True, timeout: float | None = None) -> None:
        with self._lock:
            if new_turn:
                self._gen += 1
                self._cancel_answer()
                self.speaker.stop()
                chime("wake")
            self._trigger = trigger
            self._listen_started = time.perf_counter()
            self.collector.start(listen_timeout=timeout or self.settings.listen_timeout)
            self._set_state(State.LISTENING)
            if new_turn:
                self._progress("在听…", start=True)
            else:
                self._progress("等你回答：确认 / 取消")

    def _progress(self, text: str, *, start: bool = False, done: bool = False) -> None:
        try:
            self.on_progress(Progress(text, start=start, done=done))
        except Exception:
            log.exception("显示进度出错")          # 界面出问题不能影响助手本身

    def _cancel_answer(self) -> None:
        if self._answer is not None:
            _resolve(self._answer, None)
        self._answer = None

    # ── 耳朵线程 ──────────────────────────────────────────

    def _ear_loop(self) -> None:
        while not self._quit.is_set():
            if self.state is State.PAUSED:
                self._hotkey_pressed.clear()
                time.sleep(0.1)
                continue
            if not self.mic.alive:
                self._recover_mic()
                continue

            if self._hotkey_pressed.is_set():
                self._hotkey_pressed.clear()
                if self.state in (State.IDLE, State.SPEAKING):
                    self._begin_listening("快捷键")

            frame = self.mic.read()
            if frame is None:
                continue
            try:
                self._on_frame(frame)
            except Exception:
                log.exception("处理音频出错")
                if self.state is not State.PAUSED:
                    self._set_state(State.IDLE)

    def _on_frame(self, frame) -> None:
        state = self.state
        if state is not State.LISTENING:
            # 唤醒词检测一直喂着（保持它的内部状态连续），只在待命和说话时才响应
            hit = self.wake.feed(frame)
            if hit and state in (State.IDLE, State.SPEAKING):
                log.info("唤醒：%s", hit)
                self._begin_listening(hit)
            return

        answer = self._answer                   # 不为 None 时，这次听的是对"确认吗"的回答
        outcome = self.collector.feed(frame)
        if outcome == "timeout":
            log.info("没听到说话")
            if answer is not None:
                self._set_state(State.THINKING)
                _resolve(answer, None)
            else:
                chime("cancel")
                self._set_state(State.IDLE)
                self._progress("没听到说话", done=True)
            return
        if outcome != "done":
            return

        heard_at = time.perf_counter()
        text = self.transcribe(self.collector.audio())
        asr_ms = round((time.perf_counter() - heard_at) * 1000)
        log.info("听到：%s（识别 %d ms）", text, asr_ms)
        self._set_state(State.THINKING)

        if answer is not None:
            _resolve(answer, text)
            return
        if not text:
            chime("cancel")
            self._set_state(State.IDLE)
            self._progress("没听清", done=True)
            return
        self._progress(f"听到「{text}」，在想…")
        asyncio.run_coroutine_threadsafe(
            self._handle_turn(text, self._gen, self._trigger, asr_ms, heard_at), self.loop
        )

    def _recover_mic(self) -> None:
        try:
            self.mic.restart()
            log.info("麦克风已重新打开")
        except MicrophoneError as exc:
            log.error("%s（3 秒后重试）", exc)
            self._quit.wait(3)

    # ── 大脑线程 ──────────────────────────────────────────

    async def _handle_turn(self, text: str, gen: int, trigger: str, asr_ms: int, heard_at: float) -> None:
        result = None
        error = None
        try:
            async with self._turn_lock:
                result = await self.runner.run_turn(text)
            reply = result.reply
        except Exception as exc:
            log.exception("思考失败")
            error = f"{type(exc).__name__}: {exc}"
            network = "Connection" in type(exc).__name__ or "Timeout" in type(exc).__name__
            reply = "网络不通，没法思考。" if network else "出了点问题，没办成。"
        tools = result.tools_called if result else []
        usage = result.usage if result else None
        think_ms = round((time.perf_counter() - heard_at) * 1000)
        log.info("回复：%s %s（%d ms）", reply, tools or "", think_ms)

        cost = 0.0
        if usage:
            cost = self.pricing.cost(usage.cache_hit_tokens, usage.cache_miss_tokens,
                                     usage.completion_tokens, datetime.now())

        first_sound = [0.0]
        spoke_at = time.perf_counter()
        finished = await self._say(reply, gen, first_sound)
        waited = (first_sound[0] or time.perf_counter()) - heard_at     # 你说完到听见回答，等了多久
        self.events.write(
            trigger=trigger, heard=text, asr_ms=asr_ms, think_ms=think_ms, tools=tools,
            reply=reply, speak_ms=round((time.perf_counter() - spoke_at) * 1000),
            wait_ms=round(waited * 1000), llm_ms=result.llm_ms if result else [],
            synth_ms=self.speaker.timing["synth_ms"], play_ms=self.speaker.timing["play_ms"],
            tts=self.speaker.timing["engine"],
            prompt_tokens=usage.prompt_tokens if usage else 0,
            completion_tokens=usage.completion_tokens if usage else 0,
            cache_hit_tokens=usage.cache_hit_tokens if usage else 0,
            cost=round(cost, 6), interrupted=not finished, error=error,
        )
        if gen == self._gen:
            parts = [f"等了 {waited:.1f} 秒"]
            if usage:
                parts.append(f"{format_tokens(usage.prompt_tokens + usage.completion_tokens)} token")
                if self.pricing.known:
                    parts.append(format_cost(cost, self.pricing))
            self._progress(" · ".join(parts), done=True)
        self._set_state(State.IDLE, gen)

    async def _say(self, text: str, gen: int, first_sound: list[float] | None = None) -> bool:
        """念一句。first_sound 不为空时，在真正出声的那一刻记下时间（算"等了多久"用）。"""
        self._set_state(State.SPEAKING, gen)
        if gen != self._gen:
            return False
        self._progress("准备说话…")

        def on_start():
            if first_sound is not None:
                first_sound[0] = time.perf_counter()
            if gen == self._gen:
                self._progress(f"在说：{text}")

        try:
            return await asyncio.to_thread(self.speaker.say, text, on_start)
        except SpeechError as exc:
            # 按要求不换成系统语音：念不出来就把这句话显示在气泡里，响一声提示去看
            log.warning("%s，这句没念：%s", exc, text)
            chime("cancel")
            if gen == self._gen:
                self._progress(f"（网络不好，没念出来）{text}")
            return True

    async def _confirm(self, tool: Tool, args: dict) -> bool:
        """runner 在执行危险操作前调这里：念一句"确定要……吗"，叮一声，再听一句回答。

        没听清（没说话、或听不出同不同意）就再问一次；第二次还不清楚，当作不同意。
        """
        gen = self._gen
        question = tool.confirm_prompt(args)
        verdict = None
        for attempt in range(2):
            prompt = question if attempt == 0 else f"没听清。{question}请说确认或者取消。"
            if not await self._say(prompt, gen) or gen != self._gen:
                return False                     # 问到一半被打断了，当作不同意
            answer = await self._hear_answer(gen)
            verdict = classify_answer(answer)
            log.info("确认 %s：%s → %s", tool.name, answer,
                     {True: "同意", False: "不同意", None: "没听清"}[verdict])
            if verdict is not None or gen != self._gen:
                break
        self._set_state(State.THINKING, gen)
        return bool(verdict) and gen == self._gen

    async def _hear_answer(self, gen: int) -> str | None:
        """叮一声提示"该你说了"，然后收一句话。超时或被新的一轮打断返回 None。"""
        future: concurrent.futures.Future = concurrent.futures.Future()
        with self._lock:
            if gen != self._gen:
                return None
            self._answer = future
            chime("wake")
            self._begin_listening("确认", new_turn=False, timeout=self.settings.confirm_timeout)
        try:
            return await asyncio.wait_for(asyncio.wrap_future(future), self.settings.confirm_timeout + 15)
        except asyncio.TimeoutError:
            return None
        finally:
            with self._lock:
                if self._answer is future:
                    self._answer = None
