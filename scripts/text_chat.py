"""打字代替说话，单独调试"大脑 + 工具"。

用法
    python scripts/text_chat.py             打字聊
    python scripts/text_chat.py --speak     回复同时念出来（顺便测 TTS）
    python scripts/text_chat.py -c "音量调到 30" -c "再大一点"    跑完这几句就退出

需要确认的操作（关机、关软件）会在终端里问 y/N。
"""
import argparse
import asyncio
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agent.brain import build_runner  # noqa: E402


async def confirm(tool, args) -> bool:
    answer = await asyncio.to_thread(input, f"  ? {tool.confirm_prompt(args)} [y/N] ")
    return answer.strip().lower() in ("y", "yes", "是", "确认")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--speak", action="store_true", help="把回复念出来")
    parser.add_argument("-c", "--command", action="append", help="依次执行这几句后退出")
    args = parser.parse_args()

    speaker = None
    if args.speak:
        from voice.tts import Speaker
        speaker = Speaker()

    runner = build_runner(confirm)
    print(f"工具：{', '.join(runner.tools)}")
    lines = iter(args.command) if args.command else None
    while True:
        if lines is not None:
            text = next(lines, None)
            if text is None:
                return 0
            print(f"你 > {text}")
        else:
            try:
                text = await asyncio.to_thread(input, "你 > ")
            except EOFError:
                return 0
        if text.strip() in ("exit", "quit"):
            return 0
        if not text.strip():
            continue

        started = time.perf_counter()
        result = await runner.run_turn(text)
        cost = time.perf_counter() - started
        tools = f"  [{', '.join(result.tools_called)}]" if result.tools_called else ""
        print(f"AI > {result.reply}{tools}  ({cost:.1f}s, {result.iterations} 圈)")
        if speaker:
            await asyncio.to_thread(speaker.say, result.reply)


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
