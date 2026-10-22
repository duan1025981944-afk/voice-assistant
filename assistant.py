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
from datetime import datetime
from typing import Callable

from agent.brain import build_runner
from agent.tools.base import Tool
from config.schema import VoiceSettings
from voice.hotkey import Hotkey
from voice.listen import SpeechCollector, Transcriber
from voice.mic import Microphone, MicrophoneError
from voice.paths import DATA_DIR
from voice.tts import Speaker, chime
from voice.wake import WakeWordDetector

log = logging.getLogger("assistant")

# 确认时的回答怎么算。会问确认的只有"关软件 / 关机 / 重启"，所以"关""重启"本身就算同意。
YES = ("确认", "确定", "同意", "是", "对", "好", "行", "可以", "嗯", "没问题", "没错", "当然",
       "执行", "要", "关", "重启", "yes", "yeah", "ok", "sure")
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

    >>> [classify_answer(a) for a in ["确认", "关闭吧", "关掉", "YES", "对对对", "好的关吧"]]
    [True, True, True, True, True, True]
    >>> [classify_answer(a) for a in ["不确定", "不要关", "取消", "算了吧", "No."]]
    [False, False, False, False, False]
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


class Assistant:
    def __init__(self, settings: VoiceSettings):
        self.settings = settings
        log.info("加载模型…")
        self.mic = Microphone(settings.input_device)
        self.wake = WakeWordDetector(settings.wake_words, settings.wake_threshold, settings.wake_score)
        self.collector = SpeechCollector(settings.end_silence, settings.max_speech)
        self.transcribe = Transcriber()
        self.speaker = Speaker(settings.tts_voice)
        self.runner = build_runner(self._confirm, settings)
        self.events = EventLog()

        self.state = State.IDLE
        self.on_state: Callable[[State], None] = lambda s: None     # 托盘会换成自己的
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
            return
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
        tools: list[str] = []
        error = None
        try:
            async with self._turn_lock:
                result = await self.runner.run_turn(text)
            reply, tools = result.reply, result.tools_called
        except Exception as exc:
            log.exception("思考失败")
            error = f"{type(exc).__name__}: {exc}"
            network = "Connection" in type(exc).__name__ or "Timeout" in type(exc).__name__
            reply = "网络不通，没法思考。" if network else "出了点问题，没办成。"
        think_ms = round((time.perf_counter() - heard_at) * 1000)
        log.info("回复：%s %s（%d ms）", reply, tools or "", think_ms)

        spoke_at = time.perf_counter()
        finished = await self._say(reply, gen)
        self.events.write(
            trigger=trigger, heard=text, asr_ms=asr_ms, think_ms=think_ms, tools=tools,
            reply=reply, speak_ms=round((time.perf_counter() - spoke_at) * 1000),
            interrupted=not finished, error=error,
        )
        self._set_state(State.IDLE, gen)

    async def _say(self, text: str, gen: int) -> bool:
        self._set_state(State.SPEAKING, gen)
        if gen != self._gen:
            return False
        return await asyncio.to_thread(self.speaker.say, text)

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
