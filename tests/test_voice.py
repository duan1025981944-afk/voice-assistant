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
    return None                  # 兼容旧写法：合成交给 tests/tts_helper.py


def say(_speaker, text: str) -> np.ndarray:
    from tests.tts_helper import synthesize
    return synthesize(text)


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


# ── 边收边播（不出声：用假声卡；不联网：用假的收音线程）──

def test_queue_source_gives_what_it_has():
    """解码器要 64KB 时，手里有多少先给多少，不等凑够——这是能"边收边播"的关键。"""
    import queue as q
    from voice.tts import _QueueSource
    chunks = q.Queue()
    chunks.put(b"a" * 100)
    source = _QueueSource(chunks)
    assert source.read(65536) == b"a" * 100
    chunks.put(b"b" * 10)
    chunks.put(None)
    assert source.read(65536) == b"b" * 10
    assert source.read(65536) == b"" and source.finished


def test_no_fallback_voice_when_edge_fails(monkeypatch):
    """连不上时重试一次，再不行抛 SpeechError——不会悄悄换成系统机器音。"""
    from voice import tts
    calls = []

    def broken_fetch(self, text, chunks, stop):
        calls.append(text)
        chunks.put(TimeoutError("连不上"))

    monkeypatch.setattr(tts.Speaker, "_fetch", broken_fetch)
    monkeypatch.setattr(tts.sd, "OutputStream", lambda **kw: pytest.fail("不该打开声卡"))
    with pytest.raises(tts.SpeechError):
        tts.Speaker().say("你好")
    assert calls == ["你好", "你好"]
