"""Provider 抽象层：定义"一次模型调用"在本项目里长什么样。

──────────────────────────────────────────────────────────────
这个文件是什么
──────────────────────────────────────────────────────────────
一份**契约**。它规定了四件事的统一形状：

    ToolCall    模型说"我要调这个工具" —— 长什么样
    Usage       这次请求花了多少 token —— 长什么样
    ModelReply  模型回了什么           —— 长什么样
    Provider    "调一次模型"这个动作    —— 叫什么名字、收什么参数

**为什么要有这层**：各家模型的接口不一样。DeepSeek 报告缓存命中用
prompt_cache_hit_tokens，OpenAI 用 prompt_tokens_details.cached_tokens。
如果主循环直接读这些字段，换一家厂商就得改主循环。

有了这层之后，agent/runner.py 里只有一句：

    reply = await spec.provider.chat(payload, tools=schemas)

它不知道自己在调 DeepSeek 还是别的谁。换厂商 = 写一个新的 Provider 子类
+ 改 config.json 一个字段，主循环一个字都不用动。

──────────────────────────────────────────────────────────────
这个文件不是什么
──────────────────────────────────────────────────────────────
- 它**不**含任何具体厂商的代码——那在 providers/openai_compat.py
- 它**不**负责重试、超时——那些也在具体实现里（见 openai_compat.py 的说明）
- 它**不**知道 messages 是怎么攒出来的——那是上层的事

──────────────────────────────────────────────────────────────
加一个新厂商要做什么
──────────────────────────────────────────────────────────────
1. 新建 providers/anthropic.py，写一个 class AnthropicProvider(Provider)
2. 实现 chat()，把该厂商的返回翻译成这里的 ModelReply
3. 在 providers/factory.py 的 _REGISTRY 里加一行
4. config.json 里把 kind 改成 "anthropic"

主循环、payload、compaction、session 全都不用改。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ToolCall:
    """模型的一次工具调用意图（已经解析好，与厂商无关）。

    谁会产生它
        各 Provider 的 chat() —— 把厂商返回的原始结构翻译成这个形状。

    谁会用它
        agent/runner.py 的主循环：拿 name 去工具表里查，拿 arguments 去执行，
        拿 id 把结果对回去。

    字段
        id:            调用编号，形如 "call_00_eaNuPw5gyLP6RuHktm4o0193"。
                       **回传结果时必须原样带上**，模型靠它把结果和调用对上号。
        name:          工具名，形如 "read_file"。
        arguments:     参数，**已经从 JSON 字符串解析成 dict**。
                       形如 {"path": "agent/runner.py"}。
        parse_error:   参数 JSON 解析失败时，这里记着错误原因；正常时是 None。

    为什么要有 parse_error
        模型偶尔会吐出格式坏掉的 JSON（少个括号、字段名拼错）。
        这时候不能直接崩——正确做法是把错误当成"工具执行结果"回给模型，
        让它自己重试。所以解析失败不抛异常，而是记在这个字段里，
        由 runner 统一处理（见 agent/runner.py 里的 call.parse_error 分支）。

    例子
        一次正常的调用：

        >>> call = ToolCall(id="call_1", name="read_file",
        ...                 arguments={"path": "main.py"})
        >>> call.parse_error is None
        True

        一次参数解析失败的调用：

        >>> bad = ToolCall(id="call_2", name="read_file", arguments={},
        ...                parse_error="Expecting ',' delimiter")
        >>> bad.parse_error
        "Expecting ',' delimiter"
    """

    id: str
    name: str
    arguments: dict[str, Any]
    parse_error: str | None = None


@dataclass
class Usage:
    """一次请求花了多少 token。字段名与厂商无关，由各 Provider 负责翻译。

    谁会产生它
        各 Provider 的 chat()。取不到用量数据时返回全 0 的 Usage()，
        **不报错**——用量是观察数据，不该让整轮对话失败。

    谁会用它
        agent/runner.py 每圈打印一行观察信息：实际用量、尺子误差、缓存命中率。

    字段
        prompt_tokens:      输入总量。等于 cache_hit + cache_miss。
        completion_tokens:  输出（模型这次写了多少）。
        cache_hit_tokens:   输入里命中缓存的部分。
        cache_miss_tokens:  输入里没命中的部分。

    缓存是怎么回事
        模型处理文本时会缓存已经算过的前缀。下次请求如果开头一模一样，
        这部分可以直接复用，价格差十倍（DeepSeek：命中 0.014 美元/百万 token，
        未命中 0.14）。

        命中的判断是**从第 0 个 token 开始逐个匹配**，中间改一个字，
        从那里往后全部失效。这就是为什么系统提示要放最前面、
        为什么清理上下文要"一次清够"（见 agent/payload.py 的 CLEAR_AT_LEAST）。

    例子
        >>> u = Usage(prompt_tokens=1000, completion_tokens=50,
        ...           cache_hit_tokens=896, cache_miss_tokens=104)
        >>> f"{u.hit_rate:.0%}"
        '90%'

        拿不到用量时的默认值，不会除零：

        >>> Usage().hit_rate
        0.0
    """

    prompt_tokens: int = 0
    completion_tokens: int = 0
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0

    @property
    def hit_rate(self) -> float:
        """缓存命中率，0.0 ~ 1.0。

        返回什么
            cache_hit_tokens / prompt_tokens。
            prompt_tokens 是 0 时返回 0.0（而不是抛 ZeroDivisionError）。

        拿到之后怎么用
            打印时用百分号格式：f"{usage.hit_rate:.0%}" -> "90%"。
            这个数掉下来通常意味着上下文中段被改动过（压缩、清理），
            或者刚换了系统提示。
        """
        return self.cache_hit_tokens / self.prompt_tokens if self.prompt_tokens else 0.0


@dataclass
class ModelReply:
    """模型的一次回复（已翻译成与厂商无关的形状）。

    谁会产生它
        各 Provider 的 chat()。

    谁会用它
        agent/runner.py 的主循环——整个循环的分岔就靠它的 wants_tools。

    字段
        content:     模型写的文字。**要求调工具时这里往往是 None**，别假设它一定是字符串。
        tool_calls:  模型要求调用的工具，列表。不调工具时是空列表。
                     可能有多个——模型会一次并行调五个工具。
        raw:         厂商返回的**原始 assistant 消息**，一个普通 dict。
                     runner 直接把它 append 进 messages，所以它必须是能
                     json.dumps 的纯 dict，不能是 SDK 对象（否则存档会写不出去）。
        usage:       这次请求的用量。取不到时是全 0 的 Usage()。

    为什么既有 content/tool_calls，又有 raw
        content 和 tool_calls 是**给代码看的**（好用、类型清楚）；
        raw 是**要存进 messages 发回给模型的**（必须是模型认得的原始格式）。
        两者内容重复，但用途不同——这是设计说明 §3"给模型看的 vs 给代码看的"
        那条原则的又一次体现。

    例子
        一次纯文字回复：

        >>> reply = ModelReply(content="它是主循环执行器。")
        >>> reply.wants_tools
        False

        一次要求调工具的回复：

        >>> reply = ModelReply(content=None, tool_calls=[
        ...     ToolCall(id="c1", name="read_file", arguments={"path": "a.py"})])
        >>> reply.wants_tools
        True
        >>> reply.tool_calls[0].name
        'read_file'
    """

    content: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    usage: Usage = field(default_factory=Usage)

    @property
    def wants_tools(self) -> bool:
        """模型这轮要不要用工具。

        返回什么
            True  = 要调工具，主循环应该执行它们、把结果塞回 messages、再转一圈
            False = 这就是最终答复，主循环应该退出

        为什么重要
            **这是整个 Agent 循环唯一的分岔点。** 模型每次要么"要用工具"
            （继续转），要么"说完了"（退出），没有第三种可能。
            agent/runner.py 里那句 if not reply.wants_tools: break 就是全部秘密。
        """
        return bool(self.tool_calls)


class Provider(ABC):
    """所有模型接入的统一接口（抽象基类，不能直接实例化）。

    谁会实现它
        providers/openai_compat.py 的 OpenAICompatProvider（DeepSeek、硅基流动等）。
        以后要接 Anthropic，就再写一个子类。

    谁会用它
        - agent/runner.py：转循环时调 chat()
        - agent/compaction.py：写摘要时调 chat()（不传 tools）

        它们都只认这个接口，不认具体实现。

    怎么拿到一个实例
        不要手动 new，走工厂：

            from config.loader import load_config
            from providers.factory import create_provider

            cfg = load_config()              # 读 config.json
            provider = create_provider(cfg)  # 按 cfg.kind 选实现类

    子类必须做到的四件事
        1. 把厂商返回翻译成 ModelReply（包括 ToolCall 和 Usage）
        2. raw 字段必须是能 json.dumps 的**普通 dict**，不能是 SDK 对象
        3. **重试和超时在子类内部消化掉**——对上层来说，chat() 要么成功、
           要么彻底失败，不该让"网络不稳"这件事污染主循环
        4. 拿不到 usage 时返回全 0 的 Usage()，不要抛异常
    """

    @abstractmethod
    async def chat(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ModelReply:
        """调用一次模型。

        传入什么
            messages: 要发出去的消息列表。**注意传的应该是 build_payload()
                      加工过的 payload，不是内存里那份全文**——payload 才是
                      截断、清理之后的版本。

                      形如：
                          [{"role": "system", "content": "你是一个……"},
                           {"role": "user", "content": "帮我读一下 main.py"}]

            tools:    工具定义列表，每一项是某个 Tool 的 to_schema() 输出。
                      形如：
                          [{"type": "function",
                            "function": {"name": "read_file",
                                         "description": "读取一个文本文件……",
                                         "parameters": {...}}}]

                      **不传（None）表示这轮不给工具**，模型只能用文字回答。
                      写摘要时就是这么用的——见 agent/compaction.py 的 summarize()，
                      带着 tools 的话模型有时会跑去调工具，摘要变成空的。

        返回什么
            一个 ModelReply。

        拿到之后怎么用
            标准三步（agent/runner.py 里就是这么写的）：

                reply = await provider.chat(payload, tools=schemas)
                messages.append(reply.raw)          # 先把模型说的话入链
                if not reply.wants_tools:           # 唯一的分岔点
                    break                           # 没有工具调用 = 最终答复

        会抛异常吗
            **尽量不要。** 网络抖动、限流、超时都应该在子类内部重试消化掉。
            实在失败了才抛——上层会把它当成"这一轮跪了"处理。
        """
