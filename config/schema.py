"""配置的数据形状：config.json 解析之后长什么样。

前两个类（ResolvedModel、AgentDefaults）和 mini_nanobot 的 config/schema.py 一致，
以后合并时可以直接对上。VoiceSettings 是语音助手独有的。

config.json 的结构：

    {
      "providers":    { "deepseek": { "kind": ..., "apiKey": ..., "baseUrl": ... } },
      "modelPresets": { "ds": { "provider": "deepseek", "model": "deepseek-flash" } },
      "agents":       { "defaults": { "modelPreset": "ds", "maxIterations": 6 } },
      "voice":        { "wakeWords": ["你好小智"], "hotkey": "win+alt+space", ... }
    }
"""
from dataclasses import dataclass, field


@dataclass
class ResolvedModel:
    """providers / modelPresets / agents 三块压平之后，主程序真正需要的东西。

    kind 决定"用哪段代码去调"（见 providers/factory.py），model 决定"调哪个模型"。

    例子
        >>> cfg = ResolvedModel(kind="openai_compat", api_key="sk-x",
        ...                     base_url="https://api.deepseek.com",
        ...                     model="deepseek-flash")
        >>> cfg.kind, cfg.model
        ('openai_compat', 'deepseek-flash')
    """

    kind: str
    api_key: str
    base_url: str
    model: str


@dataclass
class AgentDefaults:
    """agents.defaults 里和"用哪个模型"无关的设置。"""

    max_iterations: int


@dataclass
class VoiceSettings:
    """config.json 里 "voice" 那一块。每一项都有默认值，配置里可以一项都不写。

    字段（括号里是 config.json 里的键名）
        assistant_name     助手叫什么，会写进系统提示（assistantName）
        wake_words         唤醒词，可以有多个；3–5 个字的效果最好（wakeWords）
        wake_threshold     唤醒灵敏度，越小越容易唤醒、也越容易误唤醒（wakeThreshold）
        wake_score         唤醒词的加分，越大越容易唤醒（wakeScore）
        hotkey             按一下就开始听的快捷键（hotkey），如 "win+alt+space"
        input_device       麦克风：null 用系统默认，也可以写设备编号或名字的一部分（inputDevice）
        tts_voice          edge-tts 的音色（ttsVoice）
        confirm_timeout    问"确认吗"之后等几秒（confirmTimeoutSec）
        listen_timeout     唤醒后几秒还没开口就放弃（listenTimeoutSec）
        end_silence        说完之后停顿多久算"说完了"（endSilenceSec）
        max_speech         一句话最长录多久（maxSpeechSec）
        context_minutes    多久不说话就忘掉之前的上下文（contextMinutes）
        search_engine      网页搜索用 "bing" 还是 "baidu"（searchEngine）
        app_aliases        自定义软件别名，如 {"网易云": "网易云音乐"}（appAliases）
    """

    assistant_name: str = "小智"
    wake_words: list[str] = field(default_factory=lambda: ["你好小智"])
    wake_threshold: float = 0.25
    wake_score: float = 1.0
    hotkey: str = "win+alt+space"
    input_device: int | str | None = None
    tts_voice: str = "zh-CN-XiaoxiaoNeural"
    confirm_timeout: float = 8.0
    listen_timeout: float = 6.0
    end_silence: float = 0.7
    max_speech: float = 15.0
    context_minutes: float = 5.0
    search_engine: str = "bing"
    app_aliases: dict[str, str] = field(default_factory=dict)
