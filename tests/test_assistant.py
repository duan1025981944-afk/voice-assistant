"""整个状态机的端到端测试：合成语音当麦克风，假喇叭、假模型，不出声、不联网、不真关机。

覆盖：唤醒 → 识别 → 思考 → 回复；关机前的语音确认（说"取消" / 说"确认" / 说"关掉吧" /
没听清再问一次）。每轮的记录写进临时目录，不混进 data/events/ 里真实的使用记录。
"""
import queue
import threading
import time

import numpy as np
import pytest

from voice import paths
from voice.mic import BLOCK

pytestmark = [
    pytest.mark.models,
    pytest.mark.skipif(not paths.ASR_DIR.exists(), reason="还没下载模型"),
]


class FakeMic:
    def __init__(self):
        self.q = queue.Queue()
        self.alive = True

    def start(self): pass
    def stop(self): pass
    def restart(self): pass

    def read(self, timeout=0.2):
        try:
            return self.q.get(timeout=timeout)
        except queue.Empty:
            return None

    def push(self, audio):
        for i in range(0, len(audio), BLOCK):
            self.q.put(audio[i:i + BLOCK])


class FakeSpeaker:
    def __init__(self):
        self.said = []
        self.spoke = threading.Event()

    timing = {"synth_ms": 0, "play_ms": 0, "engine": "fake"}

    def say(self, text, on_start=None):
        if on_start:
            on_start()
        self.said.append(text)
        self.spoke.set()
        return True

    def stop(self): pass


@pytest.fixture(scope="module")
def voice_clips():
    """先把要用到的几句话合成好（16kHz）。"""
    from tests.test_voice import silence
    from tests.tts_helper import synthesize as clip
    return {
        "wake": np.concatenate([silence(0.5), clip("你好小智"), silence(0.3)]),
        "time": np.concatenate([clip("现在几点了"), silence(1.5)]),
        "shutdown": np.concatenate([clip("帮我关机"), silence(1.5)]),
        "cancel": np.concatenate([silence(0.3), clip("取消"), silence(1.5)]),
        "confirm": np.concatenate([silence(0.3), clip("确认"), silence(1.5)]),
        "close_it": np.concatenate([silence(0.3), clip("关掉吧"), silence(1.5)]),
        "unclear": np.concatenate([silence(0.3), clip("今天星期几"), silence(1.5)]),
    }


@pytest.fixture
def assistant(monkeypatch, tmp_path):
    import assistant as assistant_mod
    from config.schema import VoiceSettings
    from tests.test_runner import ScriptedProvider

    monkeypatch.setattr(assistant_mod, "chime", lambda kind="wake": None)
    a = assistant_mod.Assistant(VoiceSettings())
    a.mic, a.speaker = FakeMic(), FakeSpeaker()
    a.events.dir = tmp_path
    a.progress_log = []
    a.on_progress = a.progress_log.append
    a.runner.provider = ScriptedProvider([])
    a._brain.start()
    a._ears.start()
    yield a
    a.shutdown()


def wait_for(cond, timeout=20):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if cond():
            return True
        time.sleep(0.05)
    return False


def test_wake_then_answer(assistant, voice_clips):
    from assistant import State
    from tests.test_runner import text_reply, tool_reply
    provider = assistant.runner.provider
    provider.replies += [tool_reply("clock", {}), text_reply("现在三点")]

    assistant.mic.push(voice_clips["wake"])
    assert wait_for(lambda: assistant.state is State.LISTENING)
    assistant.mic.push(voice_clips["time"])
    assert wait_for(assistant.speaker.spoke.is_set)
    assert assistant.speaker.said == ["现在三点"]
    assert "几点" in provider.seen[0][-1]["content"]
    assert wait_for(lambda: assistant.state is State.IDLE)

    # 进度：在听 → 听到…在想 → 正在看时间 → 准备说话 → 在说 → 结束（带用时）
    steps = [p.text for p in assistant.progress_log]
    assert steps[0] == "在听…" and assistant.progress_log[0].start
    assert any(t.startswith("听到「") for t in steps)
    assert "正在看时间" in steps and "在说：现在三点" in steps
    assert assistant.progress_log[-1].done and steps[-1].startswith("等了")

    # 每轮记录里有 token 和花费（假模型的用量是 0）
    totals = assistant.events.totals()
    assert totals["turns"] == 1 and totals["tokens"] == 0


@pytest.mark.parametrize("answer, executed", [("cancel", False), ("confirm", True), ("close_it", True)])
def test_shutdown_needs_voice_confirm(assistant, voice_clips, monkeypatch, answer, executed):
    from agent.tools import power
    from assistant import State
    from tests.test_runner import text_reply, tool_reply
    sent = []
    monkeypatch.setattr(power, "_run", lambda args: sent.append(args) or 0)
    assistant.runner.provider.replies += [tool_reply("power", {"action": "shutdown"}), text_reply("好的")]

    assistant.mic.push(voice_clips["wake"])
    assert wait_for(lambda: assistant.state is State.LISTENING)
    assistant.mic.push(voice_clips["shutdown"])
    # 先问"确定要关机吗"，然后进入"听回答"
    assert wait_for(lambda: assistant._answer is not None and assistant.state is State.LISTENING)
    assert assistant.speaker.said == ["确定要关机吗？"]
    assistant.mic.push(voice_clips[answer])
    assert wait_for(lambda: len(assistant.speaker.said) == 2)
    assert bool(sent) is executed
    assert wait_for(lambda: assistant.state is State.IDLE)


def test_unclear_answer_asks_again(assistant, voice_clips, monkeypatch):
    from agent.tools import power
    from assistant import State
    from tests.test_runner import text_reply, tool_reply
    sent = []
    monkeypatch.setattr(power, "_run", lambda args: sent.append(args) or 0)
    assistant.runner.provider.replies += [tool_reply("power", {"action": "shutdown"}), text_reply("好的")]

    assistant.mic.push(voice_clips["wake"])
    assert wait_for(lambda: assistant.state is State.LISTENING)
    assistant.mic.push(voice_clips["shutdown"])
    assert wait_for(lambda: assistant._answer is not None and assistant.state is State.LISTENING)
    assistant.mic.push(voice_clips["unclear"])            # 听不出同不同意 → 再问一次
    assert wait_for(lambda: len(assistant.speaker.said) == 2 and assistant._answer is not None
                    and assistant.state is State.LISTENING)
    assert assistant.speaker.said[1].startswith("没听清")
    assert not sent
    assistant.mic.push(voice_clips["confirm"])
    assert wait_for(lambda: len(assistant.speaker.said) == 3)
    assert sent and sent[0][0] == "/s"
