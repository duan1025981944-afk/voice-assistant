"""模型文件在哪。找不到时报一个能照着做的错误，而不是 onnxruntime 的一长串堆栈。"""
from __future__ import annotations

from pathlib import Path

from config.loader import PROJECT_ROOT

MODELS_DIR = PROJECT_ROOT / "models"
DATA_DIR = PROJECT_ROOT / "data"

ASR_DIR = MODELS_DIR / "sherpa-onnx-sense-voice-zh-en-ja-ko-yue-int8-2025-09-09"
KWS_DIR = MODELS_DIR / "sherpa-onnx-kws-zipformer-wenetspeech-3.3M-2024-01-01"
VAD_FILE = MODELS_DIR / "silero_vad.onnx"
KWS_EPOCH = "epoch-12-avg-2-chunk-16-left-64"      # 官方示例用的那一组


def require(path: Path) -> str:
    if not path.exists():
        raise FileNotFoundError(
            f"缺少模型文件：{path}\n先运行：.venv\\Scripts\\python scripts\\download_models.py"
        )
    return str(path)


def asr_files() -> dict[str, str]:
    return {"model": require(ASR_DIR / "model.int8.onnx"), "tokens": require(ASR_DIR / "tokens.txt")}


def kws_files() -> dict[str, str]:
    return {
        "tokens": require(KWS_DIR / "tokens.txt"),
        "encoder": require(KWS_DIR / f"encoder-{KWS_EPOCH}.onnx"),
        "decoder": require(KWS_DIR / f"decoder-{KWS_EPOCH}.onnx"),
        "joiner": require(KWS_DIR / f"joiner-{KWS_EPOCH}.onnx"),
    }
