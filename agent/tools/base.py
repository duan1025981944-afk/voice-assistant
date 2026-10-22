"""工具基类：定义所有工具的共同形状。

──────────────────────────────────────────────────────────────
这个文件是什么
──────────────────────────────────────────────────────────────
两个基础零件：

    ToolResult   工具的返回值 —— 一个字符串，外加一个"是不是错误"的标志
    Tool         所有工具的基类 —— 说明书（给模型看）+ 实现（给代码跑）

一个工具 = **一份说明书 + 一段代码**。说明书会被翻译成 JSON 随请求发给模型，
模型据此决定"要不要用、怎么传参"；代码是真正干活的部分。

──────────────────────────────────────────────────────────────
这个文件不是什么
──────────────────────────────────────────────────────────────
- 它**不**含任何具体工具——那是同目录下的 read_file.py 等
- 它**不**负责找工具、注册工具——那是 loader.py
- 它**不**执行工具——那是 agent/runner.py

──────────────────────────────────────────────────────────────
怎么加一个新工具
──────────────────────────────────────────────────────────────
在 agent/tools/ 下新建一个 .py 文件，写一个 Tool 的子类，**就这样，
别的地方一行都不用改**（loader.py 会自动扫到它）。

    from agent.tools.base import Tool, ToolResult

    class WordCountTool(Tool):
        name = "word_count"
        description = "数一个文本文件里有多少个词。不适合二进制文件。"
        parameters = {
            "type": "object",
            "properties": {
                "path": {"type": "string",
                         "description": "相对于工作区的路径，如 README.md"},
            },
            "required": ["path"],
        }

        async def execute(self, path: str) -> str:
            ...
            return ToolResult.error("文件不存在")   # 失败这么返回
            return "一共 1234 个词"                 # 成功直接返回字符串

写 description 的两条经验（来自 ACI 设计原则）
    1. **要写反例**。不只说"能干什么"，还要说"什么时候不该用"。
       有实测：Skill 描述没有反例时选对率 73%，加上反例升到 85%。
    2. **参数要给格式和示例**。不只写类型 string，还要写
       "相对于工作区的路径，如 agent/runner.py"，模型就不用猜。
"""
from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any


class ToolResult(str):
    """工具的返回值：本质是字符串，额外挂一个"是不是错误"的标志。

    谁会产生它
        各个工具的 execute()。成功时可以直接 return 普通字符串，
        失败时统一 return ToolResult.error("……")。

    谁会用它
        agent/runner.py。它用 str(result) 拿文本塞进 messages，
        用 result.is_error 决定要不要给文本加上修正建议。

    为什么继承 str，而不是做成一个普通的类
        因为它最终要被塞进 messages 当消息内容，那里只接受字符串。
        继承 str 之后，它**本身就是**一个字符串——可以直接拼接、直接放进
        字典，不用调用方每次写 result.content。多出来的 is_error 只是
        挂在旁边给代码看的，模型看不到。

    模型看得到 is_error 吗
        **看不到。** 模型只看到文本。is_error 纯粹是给 runner 用的：
        它据此判断要不要在文本后面追加 TOOL_ERROR_HINT。

    例子
        成功的结果就是个普通字符串：

        >>> r = ToolResult("文件有 100 行")
        >>> r.is_error
        False
        >>> r == "文件有 100 行"          # 它就是字符串本身
        True

        失败的结果：

        >>> e = ToolResult.error("文件不存在")
        >>> e.is_error
        True
        >>> f"工具返回：{e}"               # 照样能直接拼接
        '工具返回：文件不存在'
    """

    is_error: bool

    def __new__(cls, content: str, *, is_error: bool = False) -> "ToolResult":
        """构造一个 ToolResult。

        传入什么
            content:  要返回给模型的文本。
            is_error: 是不是错误。**必须用关键字传**（前面那个 * 的作用），
                      写 ToolResult("abc", True) 会报错——这是故意的，
                      防止看到一个裸 True 猜不出什么意思。

        为什么写在 __new__ 里而不是 __init__ 里
            str 是**不可变类型**。不可变类型的对象在 __new__ 阶段就定型了，
            __init__ 跑的时候已经来不及影响字符串内容。所以继承 str 时，
            自定义构造必须写在 __new__ 里。
        """
        obj = str.__new__(cls, content)
        obj.is_error = is_error
        return obj

    @classmethod
    def error(cls, content: str) -> "ToolResult":
        """构造一个错误结果。工具里统一用这个返回失败。

        传入什么
            content: 错误说明。**要写清楚下一步能做什么**，不要只说"失败了"。

                差：  "Error"
                好：  "文件不存在：agnet/runner.py（工作区里没有这个路径）"

            runner 还会在后面统一追加一句 TOOL_ERROR_HINT
            （"检查参数是否正确，或换一种方式完成任务。不要用同样的参数重试。"），
            所以这里只写"发生了什么"就够，不用自己写建议。

        返回什么
            一个 is_error=True 的 ToolResult。

        为什么工具不该抛异常
            抛异常会中断整个循环，Agent 就只剩"成功或崩溃"两条路。
            把错误当成文本回给模型，它能自己换个参数重试、换个思路、
            或者老实告诉用户做不到。**这是 Agent 能自愈的根本机制。**

        例子
            >>> ToolResult.error("文件不存在").is_error
            True
        """
        return cls(content, is_error=True)


class Tool(ABC):
    """所有工具的基类（抽象基类，不能直接实例化）。

    谁会继承它
        agent/tools/ 下的每一个具体工具：EchoTool、ReadFileTool、
        WriteFileTool、ListDirTool。

    谁会用它
        - agent/tools/loader.py：扫描目录时靠 issubclass(x, Tool) 认出工具
        - agent/runner.py：拿到工具实例后调 execute()，拿 to_schema() 发给模型

    子类要提供四样东西
        name         工具名，模型调用时用的标识。必须全局唯一（loader 会查重）。
                     用小写加下划线，如 "read_file"。

        description  给模型看的说明。**要写反例**——说清楚什么时候不该用它。

        parameters   参数定义，JSON Schema 格式。形如：

                         {"type": "object",
                          "properties": {
                              "path": {"type": "string",
                                       "description": "相对工作区的路径，如 README.md"},
                          },
                          "required": ["path"]}

        execute      实现。async 函数，参数名要和 parameters 里的对得上。

    前三样为什么用类属性，不用 @property
        写起来更短：name = "read_file" 比写一个 property 少四行。
        代价是 ABC 不再强制子类必须填——所以 loader.py 里补了检查，
        谁漏了 name 或 description，启动时就会报错。

    可选的第五样：auto_discover（第 40 讲）
        默认 True：loader 启动时会空手造一个（attr()），自动登记。
        构造时必须传参数的工具，把它设成 False，loader 就跳过它，
        由别处负责造（比如 MCPTool：要等连上 server 才造得出来）。

    可选的第六样：read_only（改造 5）
        这个工具会不会改动什么。默认 False，也就是"会改"：
        runner 执行前会先问用户同不同意。只读的工具（读文件、列目录、搜文字）
        要**自己**声明 read_only = True，才能免掉这一问。

        为什么默认是"会改"
            新写一个工具忘了声明时，两种默认值的后果不一样：
                默认 False   忘了标的只读工具，每次多问一句        烦，但安全
                默认 True    忘了标的写入工具，悄悄就执行了        省事，但危险
            宁可多问。MCP server 的工具也走这个默认值：别人写的工具，先当它会改东西。
            nanobot 的 Tool.read_only 默认也是 False。

    例子
        >>> class Fake(Tool):
        ...     name = "fake"
        ...     description = "一个假工具"
        ...     parameters = {"type": "object", "properties": {}}
        ...     async def execute(self, **kwargs):
        ...         return "ok"
        >>> Fake().to_schema()["function"]["name"]
        'fake'
    """

    # ── 说明书部分：子类用类属性覆盖 ──
    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {}

    # ── 登记方式：loader 能不能空手造我（第 40 讲）──
    # 构造时必须传参数的工具（比如 MCPTool）设成 False，loader 就跳过它。
    auto_discover: bool = True

    # ── 有没有副作用（改造 5）──
    # False = 会改动东西，runner 执行前要问用户。只读的工具自己改成 True。
    read_only: bool = False

    # ── 要不要先问用户（语音助手加的）──
    # None = 沿用上面的规则（not read_only）；True / False = 工具自己说了算。
    # 为什么要多这一项：语音助手里"调音量"会改东西，但每次都问一句"确认吗"没法用。
    # 合并回 mini_nanobot 时，runner 改成调 requires_confirm() 就行，旧工具行为不变。
    needs_confirm: bool | None = None

    def requires_confirm(self, args: dict[str, Any]) -> bool:
        """这一次调用要不要先问用户。

        默认只看类属性；同一个工具里有的动作危险、有的不危险时（比如 power 工具：
        锁屏不用问，关机要问），子类覆盖这个方法，按参数判断。

        >>> class T(Tool):
        ...     name, description, parameters = "t", "t", {"type": "object"}
        ...     async def execute(self, **kw): return ""
        >>> T().requires_confirm({})          # 默认 read_only=False → 要问
        True
        >>> T.needs_confirm = False
        >>> T().requires_confirm({})
        False
        """
        if self.needs_confirm is not None:
            return self.needs_confirm
        return not self.read_only

    def confirm_prompt(self, args: dict[str, Any]) -> str:
        """问用户时念的那句话。子类可以覆盖成更具体的说法，如"确定要关机吗"。"""
        return f"确定要执行 {self.name} 吗？"

    # ── 实现部分：子类必须实现 ──
    @abstractmethod
    async def execute(self, **kwargs: Any) -> str:
        """执行工具，真正干活的地方。

        谁会调它
            agent/runner.py，用 await tool.execute(**call.arguments) 的形式。
            也就是说，**模型给的参数会被拆开当关键字参数传进来**，
            所以子类的参数名必须和 parameters 里声明的一致。

        传入什么
            由各个工具自己定义。写子类时建议写成具名参数
            （async def execute(self, path: str)），别一律用 **kwargs——
            具名参数在传错时会直接报 TypeError，比在函数体里手动检查省事。

        返回什么
            成功：一个字符串（普通 str 就行），内容会原样塞进 messages 给模型看。
            失败：ToolResult.error("……")。

        **不要抛异常。** 错误要作为文本回给模型，让它自己想办法。
        （runner 里虽然有兜底的 try/except，但那是最后一道保险，
        不是让工具偷懒的理由——兜底产生的错误信息远不如工具自己写的具体。）

        为什么是 async
            因为有些工具要等网络、等子进程。目前这四个工具其实都是同步操作，
            写成 async 只是为了统一接口，将来接 MCP 工具时不用改 runner。
        """
        ...

    # ── 翻译层：写一次，所有工具共享 ──
    def to_schema(self) -> dict[str, Any]:
        """把说明书转成 OpenAI Function Calling 要求的 JSON 格式。

        谁会用它
            agent/runner.py，在循环外算一次：

                schemas = [t.to_schema() for t in spec.tools.values()] or None

            循环外算是因为工具列表每圈都一样，没必要重复算。
            末尾那个 or None 是为了"一个工具都没有时传 None 而不是空列表"——
            有些接口不接受空列表。

        传入什么
            无。它读的是 self.name / description / parameters。

        返回什么
            一个字典，形如：

                {"type": "function",
                 "function": {"name": "read_file",
                              "description": "读取一个文本文件……",
                              "parameters": {"type": "object", ...}}}

        它要花多少 token
            实测这个项目 4 个工具的 schema 合起来约 603 token，**每一圈都要发**。
            工具越多这个固定成本越高——学习笔记里提过，5 个 MCP 服务器可能带来
            约 55,000 token 的工具定义开销。所以工具要少而精，不要贪多。
        """
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }
