"""语音版的精简主循环：请求模型 → 要不要用工具 → （先问用户）→ 执行 → 下一圈。

──────────────────────────────────────────────────────────────
和 mini_nanobot 的 agent/runner.py 比，少了什么、多了什么
──────────────────────────────────────────────────────────────
少了：压缩、payload 截断、会话存档、长期记忆。
      语音指令一句话就几个字，几分钟不说话就该忘掉，这些都用不上。

多了：
  1. confirm 是 **async** 的。语音确认要"念一句话、再听一句话"，
     期间不能卡住事件循环；终端版（scripts/text_chat.py）用 to_thread 包一下 input()。
  2. 问不问用户看 tool.requires_confirm(args)，而不是只看 read_only
     （见 agent/tools/base.py 的 needs_confirm）。
  3. 短期上下文：context_seconds 内的对话会带上，"音量调到 30""再大一点"才接得上；
     超过就清空。

一轮失败（网络断了、模型报错）时，这一轮加进 messages 的东西全部撤掉，
不留下"assistant 要调工具、但没有对应 tool 结果"的半截消息——
否则下一轮请求会被接口直接拒绝。这和 mini_nanobot "整轮写盘"是同一个道理。
"""
from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from agent.tools.base import Tool, ToolResult
from providers.base import Provider, ToolCall

# 收 (工具, 参数)，返回用户同不同意
ConfirmFn = Callable[[Tool, dict], Awaitable[bool]]

TOOL_ERROR_HINT = "（检查参数是否正确，或换一种方式完成。不要用同样的参数重试。）"
DECLINED = "用户没有同意，操作已取消。不要再试，简短告诉用户已经取消了。"

# 只保留最近这么多条消息（之前的从一条 user 消息处整段切掉）
MAX_MESSAGES = 40


@dataclass
class TurnResult:
    """一轮对话的结果。reply 是要念出来的话。"""

    reply: str
    tools_called: list[str] = field(default_factory=list)
    iterations: int = 0


class VoiceRunner:
    """持有短期上下文的对话循环。一个助手进程里只有一个实例。"""

    def __init__(
        self,
        provider: Provider,
        tools: dict[str, Tool],
        system_prompt: str,
        *,
        confirm: ConfirmFn | None = None,
        max_iterations: int = 6,
        context_seconds: float = 300,
        clock: Callable[[], float] = time.monotonic,
    ):
        self.provider = provider
        self.tools = tools
        self.system_prompt = system_prompt
        self.confirm = confirm
        self.max_iterations = max_iterations
        self.context_seconds = context_seconds
        self._clock = clock                      # 测试里换成假时钟
        self._schemas = [t.to_schema() for t in tools.values()] or None
        self.messages: list[dict[str, Any]] = []
        self._last_active = float("-inf")

    async def run_turn(self, user_text: str) -> TurnResult:
        """处理用户说的一句话，返回要念出来的回复。网络等错误会往上抛。"""
        if self._clock() - self._last_active > self.context_seconds:
            self.messages.clear()                # 隔太久了，之前说的不算数
        self._trim()

        boundary = len(self.messages)
        self.messages.append({"role": "user", "content": user_text})
        result = TurnResult(reply="")
        try:
            for i in range(1, self.max_iterations + 1):
                result.iterations = i
                payload = [{"role": "system", "content": self.system_prompt}, *self.messages]
                reply = await self.provider.chat(payload, tools=self._schemas)
                self.messages.append(reply.raw)
                if not reply.wants_tools:
                    result.reply = (reply.content or "").strip() or "好的。"
                    break
                for call in reply.tool_calls:
                    result.tools_called.append(call.name)
                    output = await self._execute(call)
                    self.messages.append(
                        {"role": "tool", "tool_call_id": call.id, "content": str(output)}
                    )
            else:
                result.reply = "这件事步骤太多了，我先停在这里。"
        except BaseException:
            del self.messages[boundary:]         # 整轮撤销，不留半截
            raise
        self._last_active = self._clock()
        return result

    async def _execute(self, call: ToolCall) -> str:
        if call.parse_error:
            return ToolResult.error(f"{call.parse_error}{TOOL_ERROR_HINT}")
        tool = self.tools.get(call.name)
        if tool is None:
            return ToolResult.error(f"没有叫 {call.name} 的工具。可用的有：{', '.join(self.tools)}")

        if self.confirm and tool.requires_confirm(call.arguments):
            if not await self.confirm(tool, call.arguments):
                return DECLINED

        try:
            output = await tool.execute(**call.arguments)
        except TypeError as exc:                 # 参数名对不上
            return ToolResult.error(f"参数不对：{exc}{TOOL_ERROR_HINT}")
        except Exception as exc:                 # 兜底：工具本该自己返回错误，而不是抛出来
            return ToolResult.error(f"{type(exc).__name__}: {exc}{TOOL_ERROR_HINT}")
        if getattr(output, "is_error", False):
            return ToolResult.error(f"{output}{TOOL_ERROR_HINT}")
        return output

    def _trim(self) -> None:
        """消息太多时从前面切掉，切口必须落在一条 user 消息上，保证工具调用和结果成对。"""
        if len(self.messages) <= MAX_MESSAGES:
            return
        cut = len(self.messages) - MAX_MESSAGES
        while cut < len(self.messages) and self.messages[cut]["role"] != "user":
            cut += 1
        del self.messages[:cut]
