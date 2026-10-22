"""读 config.json，交出主程序能直接用的配置对象。

从 mini_nanobot 的 config/loader.py 精简而来：留下 ${ENV_VAR} 展开和
providers → modelPresets → agents 的三步解析，去掉了 MCP 部分，加上 voice 一块。

找配置文件的顺序
    1. 调用方传进来的路径（测试用）
    2. 项目根目录的 config.json
    3. 都没有就用 config.example.json —— 它里面的密钥写的是 ${DEEPSEEK_API_KEY}，
       没有真实密钥，所以不复制一份也能直接跑
"""
import json
import os
import re
from pathlib import Path

from .schema import AgentDefaults, Pricing, ResolvedModel, VoiceSettings

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = PROJECT_ROOT / "config.json"
EXAMPLE_FILE = PROJECT_ROOT / "config.example.json"

_ENV_PATTERN = re.compile(r"\$\{([A-Z_][A-Z0-9_]*)\}")

# config.json 里的键名 → VoiceSettings 的字段名
_VOICE_KEYS = {
    "assistantName": "assistant_name",
    "wakeWords": "wake_words",
    "wakeThreshold": "wake_threshold",
    "wakeScore": "wake_score",
    "hotkey": "hotkey",
    "inputDevice": "input_device",
    "ttsVoice": "tts_voice",
    "ttsRate": "tts_rate",
    "ttsPitch": "tts_pitch",
    "confirmTimeoutSec": "confirm_timeout",
    "listenTimeoutSec": "listen_timeout",
    "endSilenceSec": "end_silence",
    "maxSpeechSec": "max_speech",
    "contextMinutes": "context_minutes",
    "searchEngine": "search_engine",
    "appAliases": "app_aliases",
    "showStatus": "show_status",
}

_PRICING_KEYS = {
    "currency": "currency",
    "cacheHit": "cache_hit",
    "cacheMiss": "cache_miss",
    "output": "output",
    "peakMultiplier": "peak_multiplier",
    "peakHours": "peak_hours",
}


def _expand_env(value):
    """把配置里的 ${VAR} 换成环境变量的值，递归处理嵌套结构。缺变量就当场报错。

    不用 os.path.expandvars：它在变量不存在时静默保留原样，密钥会变成字符串
    "${DEEPSEEK_API_KEY}" 被发出去，报一个看不懂的鉴权失败。

    >>> os.environ["DEMO_KEY"] = "sk-abc"
    >>> _expand_env({"a": ["${DEMO_KEY}", 1]})
    {'a': ['sk-abc', 1]}
    """
    if isinstance(value, str):
        def repl(m):
            name = m.group(1)
            got = os.environ.get(name)
            if got is None:
                raise RuntimeError(
                    f"配置里用到了环境变量 {name}，但系统里没有设置它。\n"
                    f'PowerShell 永久设置：[Environment]::SetEnvironmentVariable("{name}", "你的值", "User")'
                )
            return got
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def _config_path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    return CONFIG_FILE if CONFIG_FILE.exists() else EXAMPLE_FILE


def _read_raw(path: str | Path | None = None, *, expand: bool = True) -> dict:
    config_path = _config_path(path)
    if not config_path.exists():
        raise FileNotFoundError(f"找不到配置文件：{config_path.absolute()}")
    raw = json.loads(config_path.read_text(encoding="utf-8"))
    return _expand_env(raw) if expand else raw


def load_config(path: str | Path | None = None) -> ResolvedModel:
    """解析出"用哪家的哪个模型"。和 mini_nanobot 的同名函数行为一致。"""
    raw = _read_raw(path)

    preset_name = raw["agents"]["defaults"]["modelPreset"]
    preset = raw["modelPresets"].get(preset_name)
    if preset is None:
        available = ", ".join(raw["modelPresets"].keys())
        raise ValueError(f"配置里没有名为 '{preset_name}' 的 preset。可用的有：{available}")

    provider_name = preset["provider"]
    provider_raw = raw["providers"].get(provider_name)
    if provider_raw is None:
        available = ", ".join(raw["providers"].keys())
        raise ValueError(
            f"preset '{preset_name}' 指向了不存在的 provider '{provider_name}'。可用的有：{available}"
        )

    return ResolvedModel(
        kind=provider_raw.get("kind", "openai_compat"),
        api_key=provider_raw["apiKey"],
        base_url=provider_raw["baseUrl"],
        model=preset["model"],
    )


def load_agent_defaults(path: str | Path | None = None) -> AgentDefaults:
    """一轮最多调几次模型。语音指令都很短，默认 6 圈就够。"""
    defaults = _read_raw(path, expand=False).get("agents", {}).get("defaults", {})
    return AgentDefaults(max_iterations=int(defaults.get("maxIterations", 6)))


def load_pricing(path: str | Path | None = None) -> Pricing:
    """当前 preset 的价格（modelPresets.<名字>.pricing）。没写就是全 0：只统计 token，不算钱。"""
    raw = _read_raw(path, expand=False)
    preset_name = raw.get("agents", {}).get("defaults", {}).get("modelPreset")
    pricing = raw.get("modelPresets", {}).get(preset_name, {}).get("pricing", {})
    unknown = set(pricing) - set(_PRICING_KEYS)
    if unknown:
        raise ValueError(f"pricing 里有不认识的配置项：{', '.join(sorted(unknown))}。可用的有：{', '.join(_PRICING_KEYS)}")
    return Pricing(**{_PRICING_KEYS[k]: v for k, v in pricing.items()})


def load_voice_settings(path: str | Path | None = None) -> VoiceSettings:
    """读 "voice" 那一块。不认识的键直接报错，免得拼错了却以为改成功了。"""
    voice = _read_raw(path, expand=False).get("voice", {})
    unknown = set(voice) - set(_VOICE_KEYS)
    if unknown:
        raise ValueError(
            f"voice 里有不认识的配置项：{', '.join(sorted(unknown))}。"
            f"可用的有：{', '.join(_VOICE_KEYS)}"
        )
    return VoiceSettings(**{_VOICE_KEYS[k]: v for k, v in voice.items()})
