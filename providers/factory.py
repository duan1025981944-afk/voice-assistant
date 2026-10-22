"""Provider 工厂：按配置里的字符串挑出对应的实现类。

──────────────────────────────────────────────────────────────
这个文件是什么
──────────────────────────────────────────────────────────────
一张**查找表**，加一个函数。它解决的问题是：

    main.py 里不该出现任何具体 Provider 的类名。

没有工厂的话，main.py 得这么写：

    provider = OpenAICompatProvider(cfg.api_key, cfg.base_url, cfg.model)
               ^^^^^^^^^^^^^^^^^^^^ 类名写死在代码里

这样"换厂商"就必须改 main.py。有了工厂之后，用哪个实现类被写成**配置里的
一个字符串**（kind 字段），代码只管查表。

    config.json  "kind": "openai_compat"
                         |
    _REGISTRY    "openai_compat" -> OpenAICompatProvider
                         |
    main.py      provider = create_provider(cfg)     <- 搜不到任何类名

──────────────────────────────────────────────────────────────
三层各解耦了什么
──────────────────────────────────────────────────────────────
    config.json           解耦"参数值"    换 key、换 base_url、换模型名
    factory.py（本文件）   解耦"类的选择"  **换厂商**
    base.py               解耦"调用方式"  主循环永远只调 provider.chat()

中间这层最容易被认为是过度设计——现在 _REGISTRY 里只有一项，工厂确实
没带来任何实际收益。当初就加它的理由是：成本只有十几行，而且**同一个
模式在 agent/tools/loader.py 里又原样用了一次**（那里是"工具名 -> 工具类"）。
判断标准是"将来会不会真的有第二项"：provider 会有，工具更会有。

──────────────────────────────────────────────────────────────
加一个新厂商要改几个地方
──────────────────────────────────────────────────────────────
    1. providers/anthropic.py            写一个 Provider 子类
    2. providers/factory.py 的 _REGISTRY 加一行
    3. config.json 里 kind 改成 "anthropic"

main.py、runner.py、payload.py、session/ 全都不用动。
"""
from config.schema import ResolvedModel

from .base import Provider
from .openai_compat import OpenAICompatProvider

# 字符串 -> 实现类。加新 provider 只需在这里加一行。
#
# 这就是"用数据代替代码"：哪个实现类被使用，写在 config.json 里，
# 不写在代码里。nanobot 的 registry.py 顶部注释说得更直接——
# "Adding a new provider: 1. Add a ProviderSpec to PROVIDERS.
#  2. Add a field to ProvidersConfig. Done."
_REGISTRY: dict[str, type[Provider]] = {
    "openai_compat": OpenAICompatProvider,
}


def create_provider(cfg: ResolvedModel) -> Provider:
    """按配置里的 kind 选出实现类并实例化。

    谁会用它
        main.py，启动时调一次：

            cfg = load_config()
            provider = create_provider(cfg)

    传入什么
        cfg: 一个 ResolvedModel（见 config/schema.py）。用到它的四个字段：

                 cfg.kind      用哪个实现类，如 "openai_compat"
                 cfg.api_key   密钥
                 cfg.base_url  接口地址
                 cfg.model     模型名，如 "deepseek-flash"

    返回什么
        一个**已经实例化好的** Provider 对象，可以直接 await provider.chat(...)。

    会抛什么
        ValueError —— kind 不在 _REGISTRY 里。错误消息会**列出所有可用的 kind**。

        为什么要列出来：配置写错是最高频的问题，而原生的 KeyError 只会说
        "'anthropc'"，看不出你该写什么。列出可用值，拼错字母的人五秒钟就能改对。
        （同样的做法在 config/loader.py 里用了两次。）

    注意：这里假设所有实现类的构造参数长得一样
        _REGISTRY[kind](api_key, base_url, model) —— 三个位置参数。
        哪天某个厂商需要别的参数（比如 Anthropic 的 API 版本号），
        这行就得改成更灵活的形式。现在只有一个实现类，不提前设计。

    例子
        >>> cfg = ResolvedModel(kind="openai_compat", api_key="sk-x",
        ...                     base_url="https://api.deepseek.com",
        ...                     model="deepseek-flash")
        >>> p = create_provider(cfg)
        >>> type(p).__name__
        'OpenAICompatProvider'

        kind 写错时，报错会告诉你有哪些可用的：

        >>> bad = ResolvedModel(kind="anthropc", api_key="", base_url="", model="")
        >>> try:
        ...     create_provider(bad)
        ... except ValueError as exc:
        ...     print("openai_compat" in str(exc))
        True
    """
    provider_cls = _REGISTRY.get(cfg.kind)
    if provider_cls is None:
        available = ", ".join(_REGISTRY.keys())
        raise ValueError(f"未知的 provider kind '{cfg.kind}'。可用的有：{available}")

    return provider_cls(cfg.api_key, cfg.base_url, cfg.model)
