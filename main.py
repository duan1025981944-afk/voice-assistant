"""启动语音助手。

用法
    python main.py              带托盘图标运行
    python main.py --console    不要托盘，日志直接打在终端里，Ctrl+C 退出（调试用）
    pythonw main.py             没有黑窗口，开机自启用的就是这个（见 scripts/install_autostart.py）

日志在 data/logs/，每一轮的耗时记录在 data/events/。
"""
import argparse
import ctypes
import logging
import logging.handlers
import sys
import time

from config.loader import load_voice_settings
from voice.mic import MicrophoneError
from voice.paths import DATA_DIR

MUTEX_NAME = "Local\\voice-assistant-single-instance"
ERROR_ALREADY_EXISTS = 183

log = logging.getLogger("main")


def setup_logging(console: bool) -> None:
    log_dir = DATA_DIR / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    handlers: list[logging.Handler] = [
        logging.handlers.RotatingFileHandler(
            log_dir / "assistant.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
        )
    ]
    if console and sys.stderr is not None:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(
        level=logging.INFO, handlers=handlers,
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s", datefmt="%H:%M:%S",
    )
    for noisy in ("httpx", "openai", "comtypes", "asyncio"):
        logging.getLogger(noisy).setLevel(logging.WARNING)


def fatal(message: str) -> int:
    """启动失败：写日志；没有终端（pythonw 开机自启）时弹个窗，不然用户根本不知道它没起来。"""
    log.error(message)
    if sys.stderr is None:
        ctypes.windll.user32.MessageBoxW(None, message, "语音助手启动失败", 0x10)
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description="本地语音助手")
    parser.add_argument("--console", action="store_true", help="不要托盘，日志打在终端里")
    args = parser.parse_args()
    setup_logging(console=args.console)

    # 同一时间只允许一个实例（开机自启了一个、又手动开一个，会抢麦克风和快捷键）
    mutex = ctypes.windll.kernel32.CreateMutexW(None, False, MUTEX_NAME)
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        return fatal("已经有一个语音助手在运行了（看看托盘图标）")

    try:
        from assistant import Assistant     # 放在这里导入：模型缺失之类的错误也能走 fatal 弹窗
        assistant = Assistant(load_voice_settings())
        assistant.start()
    except (FileNotFoundError, RuntimeError, ValueError, MicrophoneError) as exc:
        return fatal(str(exc))

    if args.console:
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        assistant.shutdown()
    else:
        from ui.tray import Tray
        Tray(assistant).run()

    ctypes.windll.kernel32.CloseHandle(mutex)
    return 0


if __name__ == "__main__":
    sys.exit(main())
