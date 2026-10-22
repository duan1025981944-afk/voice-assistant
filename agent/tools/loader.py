"""工具自动发现：扫描 tools/ 目录，找出所有可用的 Tool 子类。

──────────────────────────────────────────────────────────────
这个文件是什么
──────────────────────────────────────────────────────────────
一个**自动登记处**。程序启动时它会把 agent/tools/ 目录翻一遍，
把找到的每个 Tool 子类实例化，装进一个字典交出去。

于是"加一个新工具"这件事，就只剩**新建一个 .py 文件**——
不用在任何地方注册、不用改 main.py、不用改 runner.py。

──────────────────────────────────────────────────────────────
这个文件不是什么
──────────────────────────────────────────────────────────────
- 它**不**执行工具，只负责找到并实例化
- 它**不**知道具体有哪些工具（正是这一点让加工具不用改代码）

──────────────────────────────────────────────────────────────
它是怎么"看见"文件的
──────────────────────────────────────────────────────────────
用到两个标准库：

    pkgutil.iter_modules(包.__path__)   列出一个包下面有哪些模块（不导入）
    importlib.import_module("a.b.c")    按**字符串**导入模块

关键在第二个。平时写的 import agent.tools.read_file，模块名是写死在
代码里的；import_module 的参数是个**变量**，所以能"导入一个我事先不知道
名字的模块"。没有它就做不到自动发现。

找到模块之后，用 dir(module) 把里面的名字全列出来，逐个判断
"是不是一个能用的 Tool 子类"——五道筛子见 discover_tools() 的说明。
"""
import importlib
import pkgutil

import agent.tools as tools_pkg
from agent.tools.base import Tool

# 这些模块里没有工具，跳过（省一次导入，也避免误扫）。
# base 里是 Tool 基类本身，loader 就是这个文件。
# （下划线开头的模块也会跳过，比如桌面工具共用的 _win32.py。）
_SKIP = {"base", "loader"}


def discover_tools() -> dict[str, Tool]:
    """扫描 agent/tools/ 下的所有模块，返回 {工具名: 工具实例}。

    谁会用它
        main.py，在模块级初始化时调一次：TOOLS = discover_tools()。
        测试里也会调，用来拿到一份真实的工具表。

    传入什么
        无。扫哪个目录是写死的（agent.tools 包）。

    返回什么
        一个字典：键是工具名（字符串），值是**已经实例化好的**工具对象。
        形如：

            {"echo": EchoTool(),
             "list_dir": ListDirTool(),
             "read_file": ReadFileTool(),
             "write_file": WriteFileTool()}

        **按工具名排好序**，所以每次启动顺序都一样。顺序稳定有两个好处：
        发给模型的工具列表不变，能命中提示缓存；出问题时两次运行可以对比。

    拿到之后怎么用
        两种用法，runner 里都有：

            tool = tools.get(call.name)              # 按模型给的名字查
            schemas = [t.to_schema() for t in tools.values()]   # 全部转成 JSON 发给模型

    会抛什么（都是启动期的配置错误，就该当场炸掉）
        ValueError —— 三种情况：
            某个工具类没设 name
            某个工具缺 description 或 parameters
            两个工具重名

        为什么重名要报错而不是后来者覆盖：默默覆盖的话，你会发现
        "明明写了工具却调不到"，而且完全没有线索。当场报错并说出
        是哪两个类冲突，五秒钟就能定位。

    五道筛子（对应函数体里的五个 continue）
        1. not isinstance(attr, type)         不是类（常量、函数、导入的模块）
        2. not issubclass(attr, Tool)         是类但不是工具
        3. attr.__module__ != module.__name__ 是**从别处 import 进来的**
                                              （比如每个工具文件顶部都会
                                               from ...base import Tool，
                                               不筛掉的话 Tool 基类会被反复扫到）
        4. __abstractmethods__ 非空           抽象类，实例化会 TypeError
        5. not attr.auto_discover             类自己声明了"不能空手造"
                                              （比如 MCPTool：构造时要传会话和菜单，
                                               由连 MCP server 的地方负责造。第 40 讲）

        第 3 条最容易漏。判断依据是"这个类是在哪个模块里**定义**的"——
        attr.__module__ 记的是定义处，不是导入处。

    例子
        >>> tools = discover_tools()
        >>> "volume" in tools
        True
        >>> list(tools) == sorted(tools)          # 返回值按名字排序
        True
    """
    found: dict[str, Tool] = {}

    # ① 列出 tools 包下的所有模块名（这一步只看目录，不导入）
    for _finder, module_name, _ispkg in pkgutil.iter_modules(tools_pkg.__path__):
        if module_name.startswith("_") or module_name in _SKIP:
            continue

        # ② 运行时导入——模块名是变量，不是写死的
        module = importlib.import_module(f"agent.tools.{module_name}")

        # ③ 遍历模块命名空间，挑出 Tool 子类
        for attr_name in dir(module):
            attr = getattr(module, attr_name)

            if not isinstance(attr, type):
                continue                        # 不是类：常量、函数、模块
            if not issubclass(attr, Tool):
                continue                        # 不是工具
            if attr.__module__ != module.__name__:
                continue                        # 是 import 进来的（含 Tool 基类本身）
            if getattr(attr, "__abstractmethods__", None):
                continue                        # 抽象类，实例化会 TypeError
            if not attr.auto_discover:
                continue                        # 类自己说了"别空手造我"（比如 MCPTool）

            tool = attr()

            # ④ 说明书完整性检查。
            # 因为 name/description/parameters 用的是类属性而不是 @property，
            # ABC 不会强制子类必须填，所以在这里补上检查。
            if not tool.name:
                raise ValueError(f"{attr.__name__} 没有设置 name")
            if not tool.description or not tool.parameters:
                raise ValueError(f"{tool.name} 缺少 description 或 parameters")
            if tool.name in found:
                raise ValueError(
                    f"工具名冲突：{tool.name} 同时被 "
                    f"{type(found[tool.name]).__name__} 和 {attr.__name__} 使用"
                )

            found[tool.name] = tool

    return dict(sorted(found.items()))          # 排序保证顺序稳定
