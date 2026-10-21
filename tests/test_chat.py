# -*- coding: utf-8 -*-
"""对话链路与工具调用测试：stub OpenAI 客户端驱动完整请求循环。

运行：python -m pytest tests -v -o "addopts="
"""
import json
import sys
from pathlib import Path

import httpx
import pytest
from ncatbot.testing import PluginTestHarness, extract_text
from ncatbot.testing.factories.qq import private_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
    FakeToolCall,
)

pytestmark = pytest.mark.asyncio

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


async def test_reply_strips_leading_and_trailing_whitespace(tmp_path, monkeypatch):
    """模型正文首尾换行/空格在发送前被去除（日志中常见 "\\n\\n你好"）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("\n\n  你好！  \n"), "stop"))])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("你好！")
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert session[-1] == {"role": "assistant", "content": "你好！"}


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


def _tool_call_responses(intermediate_content, final_reply):
    """构造「首轮工具调用（带中间正文）→ 二轮最终回复」的响应脚本"""
    return [
        FakeResponse(FakeChoice(FakeMessage(
            content=intermediate_content,
            tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
        ), "tool_calls")),
        FakeResponse(FakeChoice(FakeMessage(content=final_reply), "stop")),
    ]


@pytest.mark.parametrize("invalid_content", ["\n", "   ", "\n\t ", ""])
async def test_invalid_intermediate_content_not_sent(tmp_path, monkeypatch, invalid_content):
    """模型在请求工具时附带「仅空白/空」的中间正文时，不应向用户发送该无效内容

    仅发送最终回复；即 send_private_msg 只出现一次且为最终正文。
    （回归：旧代码 `if assistant_msg.content:` 对非空字符串恒为真，
    导致换行/空格等无效中间内容被当作正文意外发出。）
    """
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        _configure(plugin, _tool_call_responses(invalid_content, "最终回复OK"))

        await h.inject(private_message("现在几点了", user_id=ROOT_QQ))
        await h.settle()

        # 只能有最终回复这一条对外发送，不能多出一条空白中间内容
        h.assert_api("send_private_msg").times(1)
        h.assert_api("send_private_msg").with_text("最终回复OK")
        texts = [extract_text(c) for c in h.assert_api("send_private_msg").calls]
        assert all(t.strip() for t in texts), f"出现了空白/空中间内容: {texts!r}"


async def test_valid_intermediate_content_still_forwarded(tmp_path, monkeypatch):
    """模型在请求工具时附带「有效正文」的中间内容时，应正常发送（不受修复影响）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        _configure(plugin, _tool_call_responses("我先查一下时间", "最终回复OK"))

        await h.inject(private_message("现在几点了", user_id=ROOT_QQ))
        await h.settle()

        # 中间有效正文 + 最终回复，各发送一次
        h.assert_api("send_private_msg").times(2)
        h.assert_api("send_private_msg").with_text("我先查一下时间")
        h.assert_api("send_private_msg").with_text("最终回复OK")


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


async def test_web_tool_blocked_when_disabled(tmp_path, monkeypatch):
    """AllowWebRequests=False 时 create_http_request 工具被拒绝并返回错误 payload"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True  # AllowWebRequests 默认保持关闭
        client = _configure(plugin, [
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "create_http_request",
                    '{"method": "GET", "url": "https://example.com", "content_format": "text"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="好的"), "stop")),
        ])

        await h.inject(private_message("帮我访问网页", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("好的")
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "AllowWebRequests" in tool_entry["content"]
        # 未启用时不应发起任何真实请求（fake 客户端只会收到两轮对话）
        assert len(client.chat.completions.calls) == 2


async def test_web_image_attached_as_vision_message(tmp_path, monkeypatch):
    """create_http_request(image) 的图片通过 user 视觉消息附加，工具历史不含 base64"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowWebRequests"] = True

        # 向插件 main 模块中实际使用的 tools 模块注入 MockTransport
        main_module = sys.modules[plugin.__class__.__module__]
        png = b"\x89PNG\r\n\x1a\n_fake_png_body"

        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(200, content=png, headers={"content-type": "image/png"})

        monkeypatch.setattr(
            main_module.tools, "_default_http_transport", httpx.MockTransport(handler))

        client = _configure(plugin, [
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "create_http_request",
                    '{"method": "GET", "url": "https://example.com/a.png", '
                    '"content_format": "image"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="图片里是一只猫"), "stop")),
        ])

        await h.inject(private_message("看看这张图", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("图片里是一只猫")

        # 第二轮请求应携带图片（OpenAI vision 格式）
        second_call_messages = client.chat.completions.calls[1]["messages"]
        vision_messages = [
            m for m in second_call_messages
            if m["role"] == "user" and isinstance(m["content"], list)
        ]
        assert len(vision_messages) == 1
        image_parts = [
            part for part in vision_messages[0]["content"] if part["type"] == "image_url"
        ]
        assert image_parts[0]["image_url"]["url"].startswith("data:image/png;base64,")

        # 工具历史只保留摘要，不能把 base64 写进上下文
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "data:image" not in tool_entry["content"]
        assert json.loads(tool_entry["content"])["data"]["type"] == "image"


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
