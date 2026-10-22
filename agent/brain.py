"""把大脑组装起来：配置 → provider → 工具 → 系统提示 → VoiceRunner。

main.py（语音）和 scripts/text_chat.py（打字）都从这里拿 runner，
区别只在于 confirm 怎么问用户。
"""
from __future__ import annotations

from config.loader import PROJECT_ROOT, load_agent_defaults, load_config, load_voice_settings
from config.schema import VoiceSettings
from agent.runner import ConfirmFn, VoiceRunner
from agent.tools.loader import discover_tools
from providers.factory import create_provider

SOUL_FILE = PROJECT_ROOT / "templates" / "SOUL.md"


def system_prompt(settings: VoiceSettings) -> str:
    # 用 replace 不用 format：提示词里出现别的花括号也不会出错
    return SOUL_FILE.read_text(encoding="utf-8").replace("{assistant_name}", settings.assistant_name)


def build_runner(confirm: ConfirmFn | None, settings: VoiceSettings | None = None) -> VoiceRunner:
    settings = settings or load_voice_settings()
    return VoiceRunner(
        create_provider(load_config()),
        discover_tools(),
        system_prompt(settings),
        confirm=confirm,
        max_iterations=load_agent_defaults().max_iterations,
        context_seconds=settings.context_minutes * 60,
    )
