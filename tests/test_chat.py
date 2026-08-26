# -*- coding: utf-8 -*-
"""对话链路与工具调用测试：stub OpenAI 客户端驱动完整请求循环。

运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import private_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
    FakeToolCall,
)

pytestmark = pytest.mark.asyncio(mode="strict")

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"


def _harness() -> PluginTestHarness:
    """构造隔离的插件测试环境"""
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


def _configure(plugin, responses):
    """在内存中开启配置并注入带响应脚本的 OpenAI stub（不持久化）"""
    plugin.config["IsConfigured"] = True
    client = FakeOpenAIClient(responses)
    plugin._default_client = client
    return client


async def test_private_chat_reply_and_history(tmp_path, monkeypatch):
    """私聊对话：模型回复经 API 发送，会话历史包含 user 与 assistant 消息"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("你好，我是AI"), "stop"))])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").called().with_text("你好，我是AI")
        # 模型收到的消息序列包含本次用户输入
        # （fake 客户端持有会话列表引用，回复追加后末项会变为 assistant，故用包含断言）
        sent_messages = client.chat.completions.calls[0]["messages"]
        assert {"role": "user", "content": "你好"} in sent_messages
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        roles = [m["role"] for m in session]
        assert roles == ["user", "assistant"]


async def test_tool_call_loop_with_system_time(tmp_path, monkeypatch):
    """函数调用循环：首轮返回 tool_calls，工具执行后二轮生成最终回复"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        client = _configure(plugin, [
            # 第一轮：模型请求调用 get_system_time（无正文）
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
            ), "tool_calls")),
            # 第二轮：模型基于工具结果生成最终回复
            FakeResponse(FakeChoice(FakeMessage(content="现在是 UTC 时间"), "stop")),
        ])

        await h.inject(private_message("现在几点了", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("现在是 UTC 时间")
        # 会话历史应包含 assistant(tool_calls) 与 tool 结果消息
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assistant_entries = [m for m in session if m["role"] == "assistant" and "tool_calls" in m]
        tool_entries = [m for m in session if m["role"] == "tool"]
        assert len(assistant_entries) == 1
        assert len(tool_entries) == 1
        assert '"status": "success"' in tool_entries[0]["content"]


async def test_memory_tool_blocked_when_disabled(tmp_path, monkeypatch):
    """AllowAccessMemory=False 时 access_memory 工具被拒绝并返回错误 payload"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True  # 默认 False 的 AllowAccessMemory 保持关闭
        client = _configure(plugin, [
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "access_memory",
                    '{"action": "add", "content": "记住我喜欢蓝色"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="好的"), "stop")),
        ])

        await h.inject(private_message("记住一件事", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("好的")
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "AllowAccessMemory" in tool_entry["content"]


async def test_too_many_tool_calls_replies_error(tmp_path, monkeypatch):
    """最后一轮仍在请求工具且无正文时，回复连续调用上限提示"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["MaxRetriesTimes"] = 1  # 只允许一轮工具调用
        plugin.config["EnableBuiltinFunctionCalling"] = True
        client = _configure(plugin, [
            # 唯一一轮：模型仍想调用工具（finish_reason != stop 且有 tool_calls）
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
            ), "tool_calls")),
        ])

        await h.inject(private_message("现在几点了", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("连续工具调用次数已达上限")
