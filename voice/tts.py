"""嘴：把文字念出来，念到一半可以被打断。

两条路
    edge-tts   微软在线合成，声音自然。要联网，会把这句话发给微软
    SAPI       Windows 自带的离线语音（中文系统一般有"慧慧"），断网时兜底

两条路都先合成出完整的波形，再一小段一小段（50 毫秒）写给声卡，
每段之间看一眼 stop 标志——这就是"能被打断"的全部秘密。
"""
from __future__ import annotations

import asyncio
import logging
import tempfile
import threading
import time
import wave
from pathlib import Path
from typing import Callable

import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

EDGE_RATE = 24000
# 实测 edge-tts 第一段声音 0.9–1.5 秒到、整句 1.1–2 秒合成完。
# 按"多久没收到新数据"判断卡住，比按总时长判断快：卡住时 2.5 秒就换 SAPI，不用等满 6 秒
EDGE_FIRST_CHUNK = 2.5
EDGE_NEXT_CHUNK = 2.0
EDGE_TOTAL = 10.0
EDGE_COOLDOWN = 300        # edge-tts 失败一次后，这么多秒内直接用 SAPI，不再每句都等超时


class Speaker:
    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural"):
        self.voice = voice
        self._stop = threading.Event()
        self._lock = threading.Lock()          # 同一时间只念一句
        self._edge_down_until = 0.0
        self.timing = {"synth_ms": 0, "play_ms": 0, "engine": ""}   # 最近一句的耗时，记进每轮记录

    def say(self, text: str, on_start: Callable[[], None] | None = None) -> bool:
        """念完返回 True，被 stop() 打断返回 False。会阻塞到念完为止。

        on_start：合成完、真正开始出声的那一刻调一下（界面从"准备说话"换成"在说"）。
        """
        text = text.strip()
        if not text:
            return True
        with self._lock:
            self._stop.clear()
            started = time.perf_counter()
            samples, rate = self.synthesize(text)
            played = time.perf_counter()
            self.timing["synth_ms"] = round((played - started) * 1000)
            if self._stop.is_set():
                return False
            if on_start:
                on_start()
            finished = self._play(samples, rate)
            self.timing["play_ms"] = round((time.perf_counter() - played) * 1000)
            return finished

    def stop(self) -> None:
        self._stop.set()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        if time.monotonic() >= self._edge_down_until:
            try:
                samples = self._edge(text)
                self.timing["engine"] = "edge"
                return samples, EDGE_RATE
            except Exception as exc:
                log.warning("edge-tts 失败（%r），%d 秒内改用系统语音", exc, EDGE_COOLDOWN)
                self._edge_down_until = time.monotonic() + EDGE_COOLDOWN
        self.timing["engine"] = "sapi"
        return self._sapi(text)

    def _edge(self, text: str) -> np.ndarray:
        import edge_tts
        import miniaudio

        async def fetch() -> bytes:
            parts = []
            stream = edge_tts.Communicate(text, self.voice).stream().__aiter__()
            wait = EDGE_FIRST_CHUNK
            while True:
                try:
                    chunk = await asyncio.wait_for(stream.__anext__(), wait)
                except StopAsyncIteration:
                    break
                wait = EDGE_NEXT_CHUNK
                if chunk["type"] == "audio":
                    parts.append(chunk["data"])
            return b"".join(parts)

        mp3 = asyncio.run(asyncio.wait_for(fetch(), EDGE_TOTAL))
        if not mp3:
            raise RuntimeError("没有收到音频")
        decoded = miniaudio.decode(
            mp3, output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1, sample_rate=EDGE_RATE
        )
        return np.frombuffer(decoded.samples, dtype=np.int16).astype(np.float32) / 32768

    def _sapi(self, text: str) -> tuple[np.ndarray, int]:
        import comtypes
        import pyttsx3

        try:
            comtypes.CoInitialize()            # SAPI 走 COM，非主线程要先初始化
        except OSError:
            pass
        engine = pyttsx3.init("sapi5")
        for v in engine.getProperty("voices"):
            if any(k in f"{v.id} {v.name}".lower() for k in ("zh-cn", "chinese", "huihui", "kangkang", "yaoyao")):
                engine.setProperty("voice", v.id)
                break
        with tempfile.TemporaryDirectory() as tmp:
            wav_path = Path(tmp) / "say.wav"
            engine.save_to_file(text, str(wav_path))
            engine.runAndWait()
            with wave.open(str(wav_path)) as w:
                rate, width = w.getframerate(), w.getsampwidth()
                raw = w.readframes(w.getnframes())
                channels = w.getnchannels()
        samples = np.frombuffer(raw, dtype=np.int16 if width == 2 else np.uint8).astype(np.float32)
        samples = samples / 32768 if width == 2 else (samples - 128) / 128
        if channels > 1:
            samples = samples.reshape(-1, channels).mean(axis=1)
        return samples, rate

    def _play(self, samples: np.ndarray, rate: int) -> bool:
        step = rate // 20
        with sd.OutputStream(samplerate=rate, channels=1, dtype="float32") as out:
            for i in range(0, len(samples), step):
                if self._stop.is_set():
                    return False
                out.write(samples[i:i + step].reshape(-1, 1))
        return True


def chime(kind: str = "wake") -> None:
    """提示音，不等它放完就返回。wake：上扬两声；cancel：下沉一声。"""
    rate = 24000
    notes = {"wake": [(880, 0.07), (1320, 0.09)], "cancel": [(520, 0.12)]}[kind]
    parts = []
    for freq, dur in notes:
        t = np.arange(int(rate * dur)) / rate
        envelope = np.minimum(1, np.minimum(t, dur - t) / 0.01)    # 两头 10ms 淡入淡出，不出爆音
        parts.append(0.25 * np.sin(2 * np.pi * freq * t) * envelope)
    sd.play(np.concatenate(parts).astype(np.float32), rate)
