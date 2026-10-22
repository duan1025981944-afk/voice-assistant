"""唤醒之后"收一句话"：VAD 判断开口和说完，SenseVoice 把录音变成文字。

SpeechCollector 的三种结局
    "done"     开口了，然后停顿超过 end_silence 秒（或说满 max_speech 秒）
    "timeout"  等了 listen_timeout 秒还没开口
    None       还在收

录音从开始收的那一刻起全部保留，交给识别时从"开口前 0.3 秒"截起——
VAD 判断开口会晚一点点，不留这一截容易吞掉第一个字。
"""
from __future__ import annotations

import numpy as np
import sherpa_onnx as so

from voice import paths
from voice.mic import SAMPLE_RATE

WINDOW = 512                     # silero VAD 每次要正好 512 个采样
LEAD_IN = int(0.3 * SAMPLE_RATE)


class SpeechCollector:
    def __init__(self, end_silence: float = 0.7, max_speech: float = 15.0):
        config = so.VadModelConfig()
        config.silero_vad.model = paths.require(paths.VAD_FILE)
        config.silero_vad.threshold = 0.5
        config.silero_vad.min_silence_duration = end_silence
        config.silero_vad.min_speech_duration = 0.2
        config.silero_vad.max_speech_duration = max_speech
        config.silero_vad.window_size = WINDOW
        config.sample_rate = SAMPLE_RATE
        config.num_threads = 1
        self._vad = so.VoiceActivityDetector(config, buffer_size_in_seconds=max_speech + 10)
        self.max_speech = max_speech
        self.start(listen_timeout=6.0)

    def start(self, listen_timeout: float) -> None:
        self._vad.reset()
        self._listen_timeout = listen_timeout
        self._chunks: list[np.ndarray] = []
        self._pending = np.empty(0, dtype=np.float32)
        self._fed = 0                 # 已经喂给 VAD 的采样数
        self._speech_at: int | None = None
        self._segment_start: int | None = None

    def feed(self, samples: np.ndarray) -> str | None:
        self._chunks.append(samples)
        self._pending = np.concatenate([self._pending, samples])
        while len(self._pending) >= WINDOW:
            self._vad.accept_waveform(self._pending[:WINDOW])
            self._pending = self._pending[WINDOW:]
            self._fed += WINDOW
            if self._speech_at is None and self._vad.is_speech_detected():
                self._speech_at = self._fed
            if not self._vad.empty():                 # 出现完整的一段 = 说完了
                self._segment_start = self._vad.front.start
                return "done"

        if self._speech_at is None and self._fed > self._listen_timeout * SAMPLE_RATE:
            return "timeout"
        if self._speech_at is not None and self._fed - self._speech_at > self.max_speech * SAMPLE_RATE:
            return "done"
        return None

    def audio(self) -> np.ndarray:
        """这次收到的录音（从开口前 0.3 秒起）。"""
        full = np.concatenate(self._chunks) if self._chunks else np.empty(0, dtype=np.float32)
        start = self._segment_start if self._segment_start is not None else (self._speech_at or 0)
        return full[max(0, start - LEAD_IN):]


class Transcriber:
    """SenseVoice：中文为主，夹几个英文软件名也认得。CPU 上一句话 0.1 秒左右。"""

    def __init__(self, num_threads: int = 2):
        self._rec = so.OfflineRecognizer.from_sense_voice(
            **paths.asr_files(), num_threads=num_threads, use_itn=True, language="auto",
        )

    def __call__(self, samples: np.ndarray) -> str:
        stream = self._rec.create_stream()
        stream.accept_waveform(SAMPLE_RATE, samples)
        self._rec.decode_stream(stream)
        return stream.result.text.strip()
