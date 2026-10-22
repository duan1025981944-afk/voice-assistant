"""对着麦克风自检。三种模式：

    python scripts/check_audio.py            列出设备，录 3 秒再放出来，看音量正不正常
    python scripts/check_audio.py --listen   说一句、出一行识别结果和耗时（Ctrl+C 退出）
    python scripts/check_audio.py --wake     只测唤醒词：每次唤醒打印一行，结束时给出次数（Ctrl+C 退出）

--wake 的验收标准（见计划阶段 2）：说 20 次至少唤醒 18 次；
开着视频聊天 10 分钟误唤醒不超过 1 次。达不到就调 config.json 的 wakeThreshold / wakeScore。
"""
import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import numpy as np  # noqa: E402
import sounddevice as sd  # noqa: E402

from config.loader import load_voice_settings  # noqa: E402
from voice.mic import PRIVACY_HINT, SAMPLE_RATE, Microphone, resolve_device  # noqa: E402


def record_and_play(device) -> int:
    print(sd.query_devices())
    print(f"\n用的麦克风：{sd.query_devices(resolve_device(device), 'input')['name']}")
    print("录 3 秒，请说话…")
    audio = sd.rec(3 * SAMPLE_RATE, samplerate=SAMPLE_RATE, channels=1, dtype="float32",
                   device=resolve_device(device))
    sd.wait()
    peak = float(np.abs(audio).max())
    print(f"最大音量 {peak:.3f}（正常说话一般在 0.05–0.8）")
    if peak == 0:
        print(f"录到的全是 0：多半是麦克风权限被关了。{PRIVACY_HINT}")
        return 1
    if peak < 0.01:
        print("声音很小：检查麦克风是不是被静音，或者在 声音设置 里把输入音量调大")
    print("放出来听听…")
    sd.play(audio, SAMPLE_RATE)
    sd.wait()
    return 0


def listen_loop(settings) -> int:
    from voice.listen import SpeechCollector, Transcriber
    mic, collector, transcribe = Microphone(settings.input_device), SpeechCollector(settings.end_silence), Transcriber()
    mic.start()
    print("说一句话，停顿一下就会出结果。Ctrl+C 退出。")
    collector.start(listen_timeout=3600)
    try:
        while True:
            frame = mic.read()
            if frame is None or collector.feed(frame) != "done":
                continue
            started = time.perf_counter()
            text = transcribe(collector.audio())
            print(f"  {text}    （识别 {(time.perf_counter() - started) * 1000:.0f} ms）")
            collector.start(listen_timeout=3600)
    except KeyboardInterrupt:
        return 0
    finally:
        mic.stop()


def wake_loop(settings) -> int:
    from voice.wake import WakeWordDetector
    mic = Microphone(settings.input_device)
    detector = WakeWordDetector(settings.wake_words, settings.wake_threshold, settings.wake_score)
    mic.start()
    print(f"说「{'、'.join(settings.wake_words)}」试试（threshold={settings.wake_threshold}, "
          f"score={settings.wake_score}）。Ctrl+C 结束。")
    hits, started = 0, time.time()
    try:
        while True:
            frame = mic.read()
            if frame is not None and (word := detector.feed(frame)):
                hits += 1
                print(f"  [{time.time() - started:6.1f}s] 第 {hits} 次：{word}")
    except KeyboardInterrupt:
        print(f"\n{(time.time() - started) / 60:.1f} 分钟里一共唤醒 {hits} 次")
        return 0
    finally:
        mic.stop()


def main() -> int:
    parser = argparse.ArgumentParser(description="麦克风自检")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--listen", action="store_true", help="连续识别，看识别准不准、快不快")
    mode.add_argument("--wake", action="store_true", help="只测唤醒词")
    args = parser.parse_args()
    settings = load_voice_settings()
    if args.listen:
        return listen_loop(settings)
    if args.wake:
        return wake_loop(settings)
    return record_and_play(settings.input_device)


if __name__ == "__main__":
    sys.exit(main())
