"""精简 runner 的行为：工具循环、确认、出错撤销、上下文过期。全部用假模型，不联网。"""
import asyncio

import pytest

from agent.runner import DECLINED, VoiceRunner
from agent.tools.base import Tool, ToolResult
from providers.base import ModelReply, Provider, ToolCall


class ScriptedProvider(Provider):
    """按顺序吐出预先写好的回复，并记下每次收到的 messages。"""

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen = []

    async def chat(self, messages, tools=None):
        self.seen.append([dict(m) for m in messages])
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def tool_reply(name, args, call_id="c1"):
    call = ToolCall(id=call_id, name=name, arguments=args)
    raw = {"role": "assistant", "content": None, "tool_calls": [
        {"id": call_id, "type": "function", "function": {"name": name, "arguments": "{}"}}]}
    return ModelReply(content=None, tool_calls=[call], raw=raw)


def text_reply(text):
    return ModelReply(content=text, raw={"role": "assistant", "content": text})


class Recorder(Tool):
    name = "rec"
    description = "记录调用"
    parameters = {"type": "object", "properties": {"x": {"type": "integer"}}}
    needs_confirm = False

    def __init__(self):
        self.calls = []

    async def execute(self, x: int = 0):
        self.calls.append(x)
        return f"ok {x}"


class Dangerous(Recorder):
    name = "danger"
    needs_confirm = True


def make_runner(replies, tools, confirm=None, clock=None):
    provider = ScriptedProvider(replies)
    runner = VoiceRunner(provider, {t.name: t for t in tools}, "系统提示",
                         confirm=confirm, clock=clock or (lambda: 0.0))
    return runner, provider


def run(coro):
    return asyncio.run(coro)


def test_tool_then_answer():
    rec = Recorder()
    runner, provider = make_runner([tool_reply("rec", {"x": 3}), text_reply("好的")], [rec])
    result = run(runner.run_turn("调一下"))
    assert result.reply == "好的"
    assert rec.calls == [3]
    assert result.tools_called == ["rec"]
    # 第二次请求里带着工具结果
    assert provider.seen[1][-1] == {"role": "tool", "tool_call_id": "c1", "content": "ok 3"}


def test_safe_tool_skips_confirm():
    asked = []

    async def confirm(tool, args):
        asked.append(tool.name)
        return True

    rec = Recorder()
    runner, _ = make_runner([tool_reply("rec", {}), text_reply("好")], [rec], confirm=confirm)
    run(runner.run_turn("x"))
    assert asked == [] and rec.calls == [0]


def test_declined_confirm_does_not_execute():
    async def confirm(tool, args):
        return False

    danger = Dangerous()
    runner, provider = make_runner([tool_reply("danger", {"x": 1}), text_reply("已取消")],
                                   [danger], confirm=confirm)
    result = run(runner.run_turn("关掉"))
    assert danger.calls == []
    assert provider.seen[1][-1]["content"] == DECLINED
    assert result.reply == "已取消"


def test_tool_error_gets_hint():
    class Broken(Recorder):
        name = "broken"

        async def execute(self, x: int = 0):
            return ToolResult.error("坏了")

    runner, provider = make_runner([tool_reply("broken", {}), text_reply("没成功")], [Broken()])
    run(runner.run_turn("x"))
    assert provider.seen[1][-1]["content"].startswith("坏了（")


def test_failure_rolls_back_whole_turn():
    rec = Recorder()
    runner, _ = make_runner([tool_reply("rec", {}), ConnectionError("断网")], [rec])
    with pytest.raises(ConnectionError):
        run(runner.run_turn("x"))
    assert runner.messages == []          # 没有留下半截的 assistant/tool 消息


def test_context_expires():
    now = [0.0]
    runner, provider = make_runner([text_reply("一"), text_reply("二"), text_reply("三")],
                                   [Recorder()], clock=lambda: now[0])
    run(runner.run_turn("第一句"))
    now[0] = 60
    run(runner.run_turn("第二句"))
    assert len(provider.seen[1]) == 4      # system + 第一轮两条 + 第二句
    now[0] = 60 + 301
    run(runner.run_turn("第三句"))
    assert len(provider.seen[2]) == 2      # 过期了，只剩 system + 第三句
