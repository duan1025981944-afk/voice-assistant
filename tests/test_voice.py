"""语音链路自检：用 TTS 合成的声音代替真人，验证 唤醒 / 断句 / 识别 都接得上。

合成的声音和真人有差别，这里过了只说明"管道是通的"，
灵敏度还要真人对着麦克风试（scripts/check_audio.py --wake）。
"""
import numpy as np
import pytest

from voice import paths
from voice.mic import BLOCK, SAMPLE_RATE

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(not paths.ASR_DIR.exists(), reason="还没下载模型"),
]


@pytest.fixture(scope="module")
def speaker():
    from voice.tts import Speaker
    return Speaker()


def to_16k(samples: np.ndarray, rate: int) -> np.ndarray:
    n = int(len(samples) * SAMPLE_RATE / rate)
    return np.interp(np.linspace(0, len(samples) - 1, n), np.arange(len(samples)), samples).astype(np.float32)


def say(speaker, text: str) -> np.ndarray:
    return to_16k(*speaker.synthesize(text))


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(seconds * SAMPLE_RATE), dtype=np.float32)


def blocks(audio: np.ndarray):
    for i in range(0, len(audio), BLOCK):
        yield audio[i:i + BLOCK]


@pytest.fixture(scope="module")
def transcriber():
    from voice.listen import Transcriber
    return Transcriber()


def test_transcribe(speaker, transcriber):
    text = transcriber(say(speaker, "打开记事本，然后把音量调到三十"))
    assert "记事本" in text and ("30" in text or "三十" in text), text   # 数字有时转阿拉伯数字，有时不转


def test_wake_word(speaker):
    from voice.wake import WakeWordDetector
    det = WakeWordDetector(["你好小智"])
    audio = np.concatenate([silence(0.5), say(speaker, "你好小智"), silence(0.8)])
    hits = [h for b in blocks(audio) if (h := det.feed(b))]
    assert hits == ["你好小智"]

    det.reset()
    audio = np.concatenate([silence(0.5), say(speaker, "今天天气真不错，我们出去走走吧"), silence(0.8)])
    assert not any(det.feed(b) for b in blocks(audio))


def test_collector_done_and_timeout(speaker, transcriber):
    from voice.listen import SpeechCollector
    col = SpeechCollector(end_silence=0.7)
    col.start(listen_timeout=6)
    audio = np.concatenate([silence(1.0), say(speaker, "下一首"), silence(2.0)])
    outcome = None
    for b in blocks(audio):
        if outcome := col.feed(b):
            break
    assert outcome == "done"
    clip = col.audio()
    assert len(clip) < len(audio) - 0.5 * SAMPLE_RATE      # 开口前的静音被截掉了
    assert "下一首" in transcriber(clip)

    col.start(listen_timeout=2)
    outcomes = [col.feed(b) for b in blocks(silence(3))]
    assert "timeout" in outcomes
