"""测试用的"嘴"：把一句话合成成 16kHz 波形，喂给唤醒和识别。

助手本身只用 edge-tts（不想要机器音）；但测试不能因为网络抖一下就挂，
所以这里 edge-tts 合成失败时退回 Windows 自带的 SAPI 语音——只在测试里这么做。
"""
from __future__ import annotations

import tempfile
import wave
from pathlib import Path

import numpy as np

from voice.mic import SAMPLE_RATE
from voice.tts import Speaker, SpeechError


def to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    n = int(len(samples) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


def synthesize(text: str) -> np.ndarray:
    try:
        return to_16k(*Speaker().synthesize(text))
    except SpeechError:
        return to_16k(*_sapi(text))


def _sapi(text: str) -> tuple[np.ndarray, int]:
    import comtypes
    import pyttsx3

    try:
        comtypes.CoInitialize()
    except OSError:
        pass
    engine = pyttsx3.init("sapi5")
    for v in engine.getProperty("voices"):
        if any(k in f"{v.id} {v.name}".lower() for k in ("zh-cn", "chinese", "huihui", "kangkang", "yaoyao")):
            engine.setProperty("voice", v.id)
            break
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "say.wav"
        engine.save_to_file(text, str(path))
        engine.runAndWait()
        with wave.open(str(path)) as w:
            rate, raw = w.getframerate(), w.readframes(w.getnframes())
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768, rate
