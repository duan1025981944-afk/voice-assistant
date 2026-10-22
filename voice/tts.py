"""嘴：用 edge-tts 把文字念出来，边收边播，念到一半可以被打断。

──────────────────────────────────────────────────────────────
边合成边播放
──────────────────────────────────────────────────────────────
edge-tts 是一段一段把 mp3 发过来的。三个角色：

    收音线程   跑 edge-tts，收到一段 mp3 就放进队列；收完放一个 None
    解码器     miniaudio.stream_any 从队列里读 mp3、吐出 0.1 秒一块的 PCM
    这个线程   拿到第一块 PCM 就打开声卡开始放，后面的边解码边写

关键在 _QueueSource.read()：解码器一次要 64KB，凑够了才给的话就等于"整句收完再放"；
手里有多少先给多少，第一块声音就能提前 0.3 秒以上出来，句子越长省得越多。

每写 50 毫秒看一眼 stop 标志——这就是"能被打断"的全部秘密。

──────────────────────────────────────────────────────────────
连不上怎么办
──────────────────────────────────────────────────────────────
不换成系统自带的机器音（按你的要求）。还没出声就失败的，马上重试一次；
两次都不行，抛 SpeechError，由调用方把这句话显示在进度气泡里。
已经开始出声后才断的，就停在那里，不重念。
"""
from __future__ import annotations

import asyncio
import logging
import queue
import threading
import time
from typing import Callable

import miniaudio
import numpy as np
import sounddevice as sd

log = logging.getLogger(__name__)

EDGE_RATE = 24000
# 实测 edge-tts 第一段 0.9–1.5 秒到、整句 1.1–2 秒收完。按"多久没收到新数据"判断卡住
EDGE_FIRST_CHUNK = 2.5
EDGE_NEXT_CHUNK = 2.0
ATTEMPTS = 2
BLOCK = EDGE_RATE // 10          # 解码器每次吐 0.1 秒
WRITE = EDGE_RATE // 20          # 每次往声卡写 50 毫秒，之间检查要不要停


class SpeechError(RuntimeError):
    """edge-tts 两次都没念出来。"""


class _NothingPlayed(Exception):
    """还没出声就失败了：可以重试。"""


class _QueueSource(miniaudio.StreamableSource):
    """给解码器读的"文件"：内容来自收音线程的队列。"""

    def __init__(self, chunks: queue.Queue):
        self.chunks = chunks
        self.buffer = b""
        self.finished = False
        self.error: BaseException | None = None

    def _take(self, item) -> None:
        if item is None:
            self.finished = True
        elif isinstance(item, BaseException):
            self.error, self.finished = item, True
        else:
            self.buffer += item

    def read(self, num_bytes: int) -> bytes:
        while not self.buffer and not self.finished:      # 手里没有就等一段
            self._take(self.chunks.get())
        while len(self.buffer) < num_bytes and not self.finished and not self.chunks.empty():
            self._take(self.chunks.get_nowait())          # 已经到了的顺手拿上，但不等
        out, self.buffer = self.buffer[:num_bytes], self.buffer[num_bytes:]
        return out


class Speaker:
    def __init__(self, voice: str = "zh-CN-XiaoxiaoNeural", rate: str = "+0%", pitch: str = "+0Hz"):
        self.voice = voice
        self.rate = rate                       # 语速，如 "+10%"
        self.pitch = pitch                     # 音调，如 "+15Hz"
        self._stop = threading.Event()
        self._lock = threading.Lock()          # 同一时间只念一句
        # 最近一句的耗时，记进每轮记录。synth_ms 是"多久出第一声"，play_ms 是之后放了多久
        self.timing = {"synth_ms": 0, "play_ms": 0, "engine": "edge"}

    def say(self, text: str, on_start: Callable[[], None] | None = None) -> bool:
        """念完返回 True，被 stop() 打断返回 False；两次都连不上抛 SpeechError。会阻塞到念完。

        on_start：真正开始出声的那一刻调一下（界面从"准备说话"换成"在说"）。
        """
        text = text.strip()
        if not text:
            return True
        with self._lock:
            self._stop.clear()
            last_error: BaseException | None = None
            for attempt in range(ATTEMPTS):
                try:
                    return self._stream(text, on_start)
                except _NothingPlayed as exc:
                    last_error = exc.__cause__ or exc
                    log.warning("edge-tts 第 %d 次没念出来：%r", attempt + 1, last_error)
                    if self._stop.is_set():
                        return False
            raise SpeechError(f"edge-tts 连不上：{last_error!r}")

    def stop(self) -> None:
        self._stop.set()

    def synthesize(self, text: str) -> tuple[np.ndarray, int]:
        """整句合成成波形（不播放）。试听样本和测试用。"""
        chunks: queue.Queue = queue.Queue()
        self._fetch(text, chunks, threading.Event())     # 自己的停止标志：不受正在念的那句影响
        source = _QueueSource(chunks)
        try:
            pcm = [np.frombuffer(b, dtype=np.int16) for b in self._decode(source)]
        except miniaudio.MiniaudioError as exc:
            source.error, pcm = source.error or exc, []
        if source.error or not pcm:
            raise SpeechError(f"edge-tts 连不上：{source.error!r}")
        return np.concatenate(pcm).astype(np.float32) / 32768, EDGE_RATE

    # ── 内部 ──────────────────────────────────────────────

    def _stream(self, text: str, on_start) -> bool:
        started = time.perf_counter()
        chunks: queue.Queue = queue.Queue()
        threading.Thread(target=self._fetch, args=(text, chunks, self._stop), name="edge-tts", daemon=True).start()
        source = _QueueSource(chunks)
        out: sd.OutputStream | None = None
        sound_at = 0.0
        try:
            try:
                for block in self._decode(source):
                    if self._stop.is_set():
                        return False
                    if out is None:                 # 第一块声音到了：打开声卡，开始放
                        sound_at = time.perf_counter()
                        self.timing["synth_ms"] = round((sound_at - started) * 1000)
                        out = sd.OutputStream(samplerate=EDGE_RATE, channels=1, dtype="float32")
                        out.start()
                        if on_start:
                            on_start()
                    samples = np.frombuffer(block, dtype=np.int16).astype(np.float32) / 32768
                    for i in range(0, len(samples), WRITE):
                        if self._stop.is_set():
                            return False
                        out.write(samples[i:i + WRITE].reshape(-1, 1))
            except miniaudio.MiniaudioError as exc:  # 一个字节都没收到时，解码器自己先报错
                source.error = source.error or exc
            if out is None:                         # 一点声音都没出
                raise _NothingPlayed() from (source.error or RuntimeError("没有收到音频"))
            if source.error:
                log.warning("edge-tts 念到一半断了：%r", source.error)
            return True
        finally:
            if out is not None:
                out.stop()
                out.close()
                self.timing["play_ms"] = round((time.perf_counter() - sound_at) * 1000)

    def _decode(self, source: _QueueSource):
        return miniaudio.stream_any(
            source, source_format=miniaudio.FileFormat.MP3,
            output_format=miniaudio.SampleFormat.SIGNED16, nchannels=1,
            sample_rate=EDGE_RATE, frames_to_read=BLOCK,
        )

    def _fetch(self, text: str, chunks: queue.Queue, stop: threading.Event) -> None:
        """收音线程：把 edge-tts 的 mp3 一段段放进队列，最后放 None；出错放异常对象。"""
        import edge_tts

        async def run():
            stream = edge_tts.Communicate(text, self.voice, rate=self.rate, pitch=self.pitch).stream()
            iterator = stream.__aiter__()
            wait = EDGE_FIRST_CHUNK
            while not stop.is_set():
                try:
                    chunk = await asyncio.wait_for(iterator.__anext__(), wait)
                except StopAsyncIteration:
                    break
                wait = EDGE_NEXT_CHUNK
                if chunk["type"] == "audio":
                    chunks.put(chunk["data"])

        try:
            asyncio.run(run())
            chunks.put(None)
        except BaseException as exc:                # 超时、断网……都交给读的一方处理
            chunks.put(exc if isinstance(exc, Exception) else RuntimeError(repr(exc)))


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
