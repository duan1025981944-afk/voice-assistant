"""唤醒词：sherpa-onnx 的关键词检测，本机离线，不用训练。

唤醒词写成中文就行（config.json 的 voice.wakeWords），启动时用 text2token 转成
模型认识的拼音声母/韵母 token，写进 data/keywords.txt，形如：

    n ǐ h ǎo x iǎo zh ì @你好小智

灵敏度两个旋钮（都在 config.json 里）：
    wakeThreshold  越小越容易唤醒，也越容易误唤醒。默认 0.25
    wakeScore      给唤醒词加分，越大越容易唤醒。默认 1.0
"""
from __future__ import annotations

import numpy as np
import sherpa_onnx as so

from voice import paths
from voice.mic import SAMPLE_RATE


def write_keywords(words: list[str], tokens_file: str) -> str:
    """把中文唤醒词转成 token 写进 data/keywords.txt，返回文件路径。"""
    token_lists = so.text2token(words, tokens=tokens_file, tokens_type="ppinyin")
    lines = [f"{' '.join(tokens)} @{word}" for word, tokens in zip(words, token_lists)]
    paths.DATA_DIR.mkdir(exist_ok=True)
    out = paths.DATA_DIR / "keywords.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return str(out)


class WakeWordDetector:
    def __init__(self, words: list[str], threshold: float = 0.25, score: float = 1.0):
        files = paths.kws_files()
        self._kws = so.KeywordSpotter(
            **files,
            keywords_file=write_keywords(words, files["tokens"]),
            num_threads=1,
            keywords_threshold=threshold,
            keywords_score=score,
        )
        self._stream = self._kws.create_stream()

    def feed(self, samples: np.ndarray) -> str | None:
        """喂一块音频。检测到唤醒词时返回它（如"你好小智"），否则 None。"""
        self._stream.accept_waveform(SAMPLE_RATE, samples)
        while self._kws.is_ready(self._stream):
            self._kws.decode_stream(self._stream)
            hit = self._kws.get_result(self._stream)
            if hit:
                self._kws.reset_stream(self._stream)
                return hit
        return None

    def reset(self) -> None:
        self._stream = self._kws.create_stream()
