"""下载语音模型到 models/（一次性，约 200MB，不进 git）。

用法
    python scripts/download_models.py           下载还没有的模型
    python scripts/download_models.py --list    只列出要下什么、多大，不下载

GitHub 慢的话，先在同一个终端里设代理再跑（urllib 会自动读这个环境变量）：
    $env:HTTPS_PROXY = "http://127.0.0.1:7890"

下载完的目录长这样：
    models/
      sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09/   识别
      sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01/      唤醒词
      silero_vad.onnx                                             断句
"""
import argparse
import sys
import tarfile
import urllib.request
from pathlib import Path

MODELS_DIR = Path(__file__).resolve().parent.parent / "models"
BASE_URL = "https://github.com/k2-fsa/sherpa-onnx/releases/download"

# (release 标签, 文件名, 字节数)。字节数用来校验"下完整了没有"。
MODELS = [
    ("asr-models", "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09.tar.bz2", 165_783_878),
    ("kws-models", "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01.tar.bz2", 32_654_866),
    ("asr-models", "silero_vad.onnx", 643_854),
]


def _target(name: str) -> Path:
    """下载并解压之后，这个模型在磁盘上的位置（压缩包解压成同名目录）。"""
    return MODELS_DIR / name.removesuffix(".tar.bz2")


def _download(url: str, dest: Path, size: int) -> None:
    """下到 .part，大小对上了才改名。中途断掉的话下次从头重下，不会留下半个文件。"""
    part = dest.with_suffix(dest.suffix + ".part")
    with urllib.request.urlopen(url, timeout=60) as resp, open(part, "wb") as f:
        done = 0
        while chunk := resp.read(1 << 20):
            f.write(chunk)
            done += len(chunk)
            print(f"\r  {done / 1e6:6.1f} / {size / 1e6:.1f} MB", end="", flush=True)
    print()
    if part.stat().st_size != size:
        part.unlink()
        raise RuntimeError(f"{dest.name} 大小不对（期望 {size} 字节），已删除，请重跑")
    part.replace(dest)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--list", action="store_true", help="只列出，不下载")
    args = parser.parse_args()

    MODELS_DIR.mkdir(exist_ok=True)
    for tag, name, size in MODELS:
        target = _target(name)
        status = "已有" if target.exists() else "缺"
        print(f"[{status}] {name}  {size / 1e6:.1f} MB")
        if args.list or target.exists():
            continue

        archive = MODELS_DIR / name
        _download(f"{BASE_URL}/{tag}/{name}", archive, size)
        if name.endswith(".tar.bz2"):
            print("  解压中…")
            with tarfile.open(archive, "r:bz2") as tar:
                tar.extractall(MODELS_DIR, filter="data")   # filter="data" 挡住 ../ 之类的越界路径
            archive.unlink()
    print("完成" if not args.list else "")
    return 0


if __name__ == "__main__":
    sys.exit(main())
