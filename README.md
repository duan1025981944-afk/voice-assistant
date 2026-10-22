# voice-assistant

本机运行的中文语音助手，体验类似 Siri：说"你好小智"或按 `Win+Alt+Space` → 说一句话 → 它听懂、动手、用语音回答。

```
你好小智 → 叮 → "打开微信"             → 微信打开了，"好的"
              → "音量调到 30"           → "好的，音量 30"
              → "再大一点"              → "音量 40"（记得上一句）
              → "关机"                  → "确定要关机吗？" → "确认" → 60 秒后关机
              → "取消关机"              → 倒计时撤销
```

- **听**全在本机：唤醒词、断句、识别（sherpa-onnx + SenseVoice），录音不离开电脑
- **想**用 DeepSeek：只有识别出来的文字会发出去
- **说**用 edge-tts（声音自然，会把回复发给微软合成）；断网时自动换成 Windows 自带语音
- 说话时喊唤醒词可以打断它

---

## 快速开始

```powershell
cd D:\VibeCoding\voice-assistant
uv venv --python 3.12 .venv
uv pip install --python .venv\Scripts\python.exe -r requirements.txt
.venv\Scripts\python scripts\download_models.py      # 约 200MB，一次性
.venv\Scripts\python main.py                          # 托盘里出现一个麦克风图标
```

API key 和 mini_nanobot 用的是同一个环境变量 `DEEPSEEK_API_KEY`，已经设过就不用再设。

开机自启（只在你的"启动"文件夹里放一个快捷方式，不需要管理员权限）：

```powershell
.venv\Scripts\python scripts\install_autostart.py           # 装
.venv\Scripts\python scripts\install_autostart.py --remove  # 卸
```

## 托盘图标

| 颜色 | 状态 |
| --- | --- |
| 灰 | 待命，等唤醒词 |
| 绿 | 在听 |
| 蓝 | 在想 |
| 橙 | 在说 |
| 红（带斜线） | 已暂停：麦克风流整个关掉，唤醒词也听不到 |

左键点一下 = 暂停 / 恢复。右键菜单可以打开日志文件夹、退出。

## 它能做的事（第一版）

| 工具 | 例句 | 要先确认吗 |
| --- | --- | --- |
| `open_app` | 打开微信 / 打开网易云 | 不用 |
| `switch_to_app` | 切到 Chrome | 不用 |
| `close_app` | 关掉记事本（等于点 ×，不强制结束） | **要** |
| `volume` | 音量调到 30 / 大一点 / 静音 | 不用 |
| `brightness` | 亮度调到 50 | 不用 |
| `media` | 暂停 / 下一首 | 不用 |
| `web_search` / `open_url` | 搜一下明天北京天气 / 打开 b 站 | 不用 |
| `power` | 锁屏 / 睡眠 / 关机 / 重启 / 取消关机 | 关机、重启**要** |
| `clock` | 几点了 | 不用 |

删除文件、执行命令、改注册表、强制结束进程：**没有这些工具**，模型想做也做不了。

## 权限说明

| 事情 | 情况 |
| --- | --- |
| 麦克风 | 要打开 设置 → 隐私和安全性 → 麦克风 →"允许桌面应用访问麦克风"。关着时 Windows 不报错，只给静音，`check_audio.py` 会提示 |
| 管理员 | **不需要，也不要用管理员身份运行**。所有工具在普通权限下都能用 |
| 以管理员身份运行的窗口 | 普通权限的程序切换不了、也关不掉它们（Windows 的 UIPI 限制），助手会如实说明 |
| UAC 弹窗 | 任何程序都点不了，系统设计如此 |
| 快捷键 | 用 `RegisterHotKey`，只监听这一个组合键，不装全局键盘钩子。被别的软件占用时日志里会提示，换 `config.json` 的 `voice.hotkey` |
| 杀毒软件 | 直接跑 Python 源码，一般不会被拦 |

## 自检

```powershell
.venv\Scripts\python -m pytest                        # 43 项，不出声、不联网、不真关机
.venv\Scripts\python scripts\check_audio.py           # 录 3 秒再放出来
.venv\Scripts\python scripts\check_audio.py --listen  # 说一句、出一行识别结果
.venv\Scripts\python scripts\check_audio.py --wake    # 只测唤醒词，统计命中次数
.venv\Scripts\python scripts\text_chat.py             # 打字代替说话，调试大脑和工具
.venv\Scripts\python main.py --console                # 不要托盘，日志直接打在终端里
```

`tests/test_voice.py` 和 `tests/test_assistant.py` 用 TTS 合成的声音当麦克风输入，验证"唤醒 → 识别 → 思考 → 回复"和"关机前语音确认"整条链路。
合成的声音和真人不一样，**唤醒灵敏度还得真人用 `--wake` 试**：说 20 次至少唤醒 18 次；开着视频 10 分钟，误唤醒不超过 1 次。

## 配置

不建 `config.json` 时直接用 `config.example.json`（里面的密钥写的是 `${DEEPSEEK_API_KEY}`，没有真实密钥）。
要改就复制一份成 `config.json` 再改，`voice` 里能写的项见 `config/schema.py` 的 `VoiceSettings`，常用的：

- `wakeWords`：唤醒词，3–5 个字效果最好，可以写多个
- `wakeThreshold` / `wakeScore`：唤醒不灵就调小 threshold、调大 score；误唤醒多就反过来
- `hotkey`：如 `"ctrl+alt+j"`
- `appAliases`：你的叫法 → 开始菜单里的正式名，如 `{"网易云": "网易云音乐"}`

## 架构

```
main.py              启动：日志、单实例、托盘
assistant.py         编排层：状态机 待命 → 在听 → 在想 → 在说（耳朵线程 + 大脑线程）
voice/               耳朵和嘴：mic / wake / listen（VAD + SenseVoice）/ tts / hotkey
agent/               大脑：runner（精简的工具循环）、brain（组装）、tools/（桌面工具）
providers/  config/  从 mini_nanobot 拷来的模型接入和配置读取
ui/tray.py           托盘图标
templates/SOUL.md    系统提示，改了就生效
data/                日志（logs/）和每轮耗时记录（events/），不进 git
models/              语音模型，不进 git
```

### 以后并进 mini_nanobot

目录和 mini_nanobot 对齐，是为了以后能直接合并：

- `agent/tools/*.py` 用的是同一个 `Tool` 基类，拷进 `mini_nanobot/agent/tools/` 就会被自动发现。
  基类只多了一个 `needs_confirm` 和两个方法（`requires_confirm(args)`、`confirm_prompt(args)`），
  mini_nanobot 的 runner 把 `not tool.read_only` 换成 `tool.requires_confirm(args)` 即可，旧工具行为不变。
- `voice/` 只对外提供"收一句话"和"念一句话"，将来就是 mini_nanobot 的语音入口；
  微信语音消息也能交给同一个 `Transcriber` 转成文字。
- 和 mini_nanobot 的差别：确认是 async 的（要念、要听），没有压缩、存档和长期记忆（语音指令用不上）。
