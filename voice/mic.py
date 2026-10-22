"""麦克风：16kHz 单声道，每 0.1 秒一块，放进队列给耳朵线程取。

权限
    需要 设置 → 隐私和安全性 → 麦克风 里"允许桌面应用访问麦克风"是开着的。
    关着的话 Windows 不会报错，而是给一串全 0 的静音——所以 check_audio.py
    会看录到的音量，全 0 就提示去开权限。
"""
from __future__ import annotations

import logging
import queue

import numpy as np
import sounddevice as sd

SAMPLE_RATE = 16000
BLOCK = SAMPLE_RATE // 10

log = logging.getLogger(__name__)

PRIVACY_HINT = "检查 设置 → 隐私和安全性 → 麦克风，打开“允许桌面应用访问麦克风”"


class MicrophoneError(RuntimeError):
    pass


def resolve_device(device: int | str | None) -> int | None:
    """None 用系统默认；数字是设备编号；字符串按名字里的字找第一个输入设备。"""
    if device is None or isinstance(device, int):
        return device
    for i, info in enumerate(sd.query_devices()):
        if info["max_input_channels"] > 0 and device.lower() in info["name"].lower():
            return i
    raise MicrophoneError(f"没有名字里带「{device}」的麦克风。可用的：\n{sd.query_devices()}")


class Microphone:
    def __init__(self, device: int | str | None = None):
        self.device = resolve_device(device)
        self._queue: queue.Queue[np.ndarray] = queue.Queue(maxsize=300)   # 最多攒 30 秒
        self._stream: sd.InputStream | None = None

    def _callback(self, indata, frames, time_info, status):
        if status:
            log.debug("麦克风状态：%s", status)
        try:
            self._queue.put_nowait(indata[:, 0].copy())
        except queue.Full:
            pass                    # 消费者卡住了，丢掉新数据，别把回调线程也卡住

    def start(self) -> None:
        if self._stream is not None:
            return
        try:
            self._stream = sd.InputStream(
                samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                blocksize=BLOCK, device=self.device, callback=self._callback,
            )
            self._stream.start()
        except sd.PortAudioError as exc:
            self._stream = None
            raise MicrophoneError(f"打不开麦克风：{exc}。{PRIVACY_HINT}") from exc

    def stop(self) -> None:
        """彻底关掉录音流（托盘里的"暂停监听"用的就是这个）。"""
        if self._stream is not None:
            self._stream.stop()
            self._stream.close()
            self._stream = None
        self.drain()

    @property
    def alive(self) -> bool:
        return self._stream is not None and self._stream.active

    def restart(self) -> None:
        """设备被拔掉、驱动重置之后用：关掉重开。"""
        self.stop()
        self.start()

    def read(self, timeout: float = 0.2) -> np.ndarray | None:
        try:
            return self._queue.get(timeout=timeout)
        except queue.Empty:
            return None

    def drain(self) -> None:
        while not self._queue.empty():
            self._queue.get_nowait()
