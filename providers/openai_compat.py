"""OpenAI 兼容接口的 Provider 实现。DeepSeek、硅基流动、Kimi、通义等都走这个。

──────────────────────────────────────────────────────────────
这个文件是什么
──────────────────────────────────────────────────────────────
一个**翻译官**。它把厂商 SDK 返回的东西，翻译成本项目内部统一的形状
（ModelReply / ToolCall / Usage，都定义在 providers/base.py）。

"OpenAI 兼容"是什么意思：很多厂商把自己的接口做成和 OpenAI 一模一样的形状，
所以用 openai 这个官方 SDK、改一下 base_url 就能调它们。
换 DeepSeek 和硅基流动之间，只要改 config.json 的 baseUrl，代码一个字不动。

──────────────────────────────────────────────────────────────
这个文件不是什么
──────────────────────────────────────────────────────────────
- 它**不**知道 messages 是怎么攒出来的，也不改它们
- 它**不**知道调用方拿这些数据干什么

──────────────────────────────────────────────────────────────
翻译时的两个坑（本文件最值得看的部分）
──────────────────────────────────────────────────────────────
**坑一：raw 不能直接用 msg.model_dump()**

raw 这个字段会被 runner 原样 append 进 messages，然后
（a）下一圈随请求发回给模型，（b）被 json.dumps 写进存档。

msg.model_dump() 会带出 refusal、annotations 等一堆 SDK 自己的字段，
回传时可能被 API 拒绝。所以这里**手工拼一份干净的 assistant 消息**，
只留 role / content / tool_calls 三样。

**坑二：arguments 要保留两份**

模型给的工具参数是一个 **JSON 字符串**，比如 '{"path": "main.py"}'。

    raw 里            原样保留字符串    <- 要发回给模型，必须和它给的一模一样
    ToolCall.arguments 解析成 dict       <- 给代码用，方便 tool.execute(**args)

两份内容相同、形态不同，用途也不同。这是"给模型看的 vs 给代码看的"
这条原则在这个文件里的体现。

──────────────────────────────────────────────────────────────
还没做的：重试与超时
──────────────────────────────────────────────────────────────
按分层约定，**网络重试和超时应该关在这个文件里**——对上层来说，
chat() 要么成功、要么彻底失败，不该让"网络不稳"污染主循环。

nanobot 的对应方法直接叫 chat_with_retry，就是这个意思。

目前这里还没实现，429 限流、502、连接超时都会直接往上抛。
记在待办里。
"""
import json

from openai import AsyncOpenAI

from .base import ModelReply, Provider, ToolCall, Usage


class OpenAICompatProvider(Provider):
    """OpenAI 兼容接口的实现。

    谁会产生它
        providers/factory.py 的 create_provider()。**不要自己 new**——
        走工厂才能保证"换厂商只改配置"。

    谁会用它
        agent/runner.py（转循环）、agent/compaction.py（写摘要）。
        它们都只认 Provider 这个抽象接口，不认这个具体类。

    实例化要什么
        api_key:  密钥。从 config.json 来，那里通常写成 ${DEEPSEEK_API_KEY}，
                  由 config/loader.py 替换成环境变量的值。
        base_url: 接口地址，如 "https://api.deepseek.com"。
                  **换厂商主要就是换这个。**
        model:    模型名，如 "deepseek-flash"。

    一个实例能反复用吗
        能。它内部持有一个 AsyncOpenAI 客户端，整个程序生命周期共用一个。
        客户端会复用底层的 HTTP 连接，比每次新建快。
    """

    def __init__(self, api_key: str, base_url: str, model: str):
        """建立与厂商接口的连接。

        传入什么
            见类文档。

        注意
            这一步**不发任何网络请求**，只是把配置记下来。
            所以密钥写错、地址写错，要等第一次 chat() 才会报错。
        """
        self.model = model
        self._client = AsyncOpenAI(api_key=api_key, base_url=base_url)

    async def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
    ) -> ModelReply:
        """调用一次模型，把返回翻译成 ModelReply。

        传入什么
            messages: 要发出去的消息列表。应该传 build_payload() 加工过的
                      payload，不是内存里那份全文。
            tools:    工具定义列表（各 Tool 的 to_schema() 输出）。
                      **None 或空列表都表示不给工具。**

        返回什么
            一个 ModelReply。四个字段都填好了：content / tool_calls / raw / usage。

        会抛什么
            openai SDK 的各种异常（限流、超时、鉴权失败等），目前直接往上抛。
            按约定这里应该重试，还没做，见文件顶部说明。

        为什么空列表也不传 tools
            kwargs 里那句 `if tools:` 会同时过滤掉 None 和 []。
            给模型一个空的 tools 字段，有些接口会报错，有些会困惑。
            干脆不传这个字段。
        """
        kwargs = {"model": self.model, "messages": messages}
        if tools:                      # 空列表和 None 都不传，别给模型一个空 tools 字段
            kwargs["tools"] = tools

        resp = await self._client.chat.completions.create(**kwargs)
        msg = resp.choices[0].message

        # ── 一、原件：手工拼一份干净的 assistant 消息 ──
        # 不用 msg.model_dump()，那会带出 refusal 等一堆字段，回传时可能被 API 拒绝
        raw: dict = {"role": "assistant", "content": msg.content}

        calls: list[ToolCall] = []
        if msg.tool_calls:
            raw["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,   # 原样保留字符串
                    },
                }
                for tc in msg.tool_calls
            ]

            # ── 二、解析好的视图：把 arguments 字符串变成 dict ──
            for tc in msg.tool_calls:
                try:
                    args = json.loads(tc.function.arguments or "{}")
                    err = None
                except json.JSONDecodeError as exc:
                    # ★ 解析失败**不抛异常**，把原因记进 ToolCall.parse_error。
                    # 模型偶尔会吐出坏掉的 JSON（少个括号、字段名拼错）。
                    # runner 看到 parse_error 会把它转成一条 tool 消息回给模型，
                    # 让它自己重写一次——这比直接崩掉有用得多。
                    args, err = {}, f"参数不是合法 JSON：{exc}"
                calls.append(
                    ToolCall(id=tc.id, name=tc.function.name, arguments=args, parse_error=err)
                )

        return ModelReply(
            content=msg.content, tool_calls=calls, raw=raw, usage=_read_usage(resp)
        )


def _read_usage(resp) -> Usage:
    """把厂商各自的用量字段翻译成统一的 Usage。

    谁会用它
        本文件的 chat()。测试里也会直接调它（tests/test_runner.py）。

    传入什么
        resp: SDK 返回的整个响应对象。这里用 getattr 逐层取值，
              所以传一个"长得像"的假对象也行——测试就是这么做的。

    返回什么
        一个 Usage。**任何一步取不到，对应字段就是 0**，绝不抛异常。

    为什么全程用 getattr 而不是直接 resp.usage.prompt_tokens
        用量是**观察数据，不是主流程**。某个兼容接口不返回 usage、
        或者字段名不一样，都不该让整轮对话失败。少一个数字而已。

    三种字段名都要认
        DeepSeek：  usage.prompt_cache_hit_tokens / prompt_cache_miss_tokens
        OpenAI：    usage.prompt_tokens_details.cached_tokens（只有命中，没有未命中）
        都没有：    命中算 0

        未命中取不到时用 `prompt - hit` 减出来——两者相加本来就等于输入总量。

    例子
        DeepSeek 风格：

        >>> class U:
        ...     prompt_tokens, completion_tokens = 1000, 50
        ...     prompt_cache_hit_tokens, prompt_cache_miss_tokens = 896, 104
        >>> class R: usage = U()
        >>> u = _read_usage(R())
        >>> (u.cache_hit_tokens, u.cache_miss_tokens)
        (896, 104)

        OpenAI 风格，未命中是减出来的：

        >>> class D: cached_tokens = 640
        >>> class U2:
        ...     prompt_tokens, completion_tokens = 1000, 50
        ...     prompt_tokens_details = D()
        >>> class R2: usage = U2()
        >>> _read_usage(R2()).cache_miss_tokens
        360

        完全没有 usage 时，返回全 0，不报错：

        >>> class R3: usage = None
        >>> _read_usage(R3()) == Usage()
        True
    """
    u = getattr(resp, "usage", None)
    if u is None:
        return Usage()

    prompt = getattr(u, "prompt_tokens", 0) or 0

    hit = getattr(u, "prompt_cache_hit_tokens", None)
    if hit is None:
        details = getattr(u, "prompt_tokens_details", None)
        hit = getattr(details, "cached_tokens", 0) if details else 0
    hit = hit or 0

    miss = getattr(u, "prompt_cache_miss_tokens", None)
    if miss is None:
        miss = max(0, prompt - hit)          # 没给未命中字段就自己减出来

    return Usage(
        prompt_tokens=prompt,
        completion_tokens=getattr(u, "completion_tokens", 0) or 0,
        cache_hit_tokens=hit,
        cache_miss_tokens=miss,
    )
