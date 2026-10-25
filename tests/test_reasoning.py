# -*- coding: utf-8 -*-
"""推理模型思维链剥离测试

模型把思维链以 ``<think>`` / ``<reasoning>`` 等标签（或 DeepSeek-R1 全角分隔符）
混入正文、或单独返回思维链时：回复、中间消息与会话历史都不得包含思维链。

运行：python -m pytest tests -v -o "addopts="
"""
import json
from pathlib import Path

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
    return PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR)


def _configure(plugin, replies):
    plugin.config["IsConfigured"] = True
    client = FakeOpenAIClient([
        FakeResponse(FakeChoice(FakeMessage(reply), "stop")) for reply in replies
    ])
    plugin._default_client = client
    return client


async def test_reasoning_stripped_from_reply_and_history(tmp_path, monkeypatch):
    """思维链不发送给用户，也不写入会话历史"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, ["<thinking>先推理一下</thinking>你好，我是AI"])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").called().with_text("你好，我是AI")
        texts = [extract_text(call) for call in h.assert_api("send_private_msg").calls]
        assert all("先推理一下" not in t for t in texts)

        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert session[-1]["content"] == "你好，我是AI"


async def test_orphan_reasoning_close_stripped(tmp_path, monkeypatch):
    """provider 已剥掉开标签、只剩 </think> 时，其前的思维链不发送给用户"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, ["我先在心里推理一遍</think>你好，我是AI"])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("你好，我是AI")
        texts = [extract_text(call) for call in h.assert_api("send_private_msg").calls]
        assert all("我先在心里推理一遍" not in t for t in texts)

        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert session[-1]["content"] == "你好，我是AI"


async def test_reasoning_only_reply_skipped(tmp_path, monkeypatch):
    """模型仅返回思维链时：不发送任何消息，也不向历史追加 assistant"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, ["<thinking>这里没有正文</thinking>"])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").not_called()
        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert [m["role"] for m in session] == ["user"]
        assert all("这里没有正文" not in json.dumps(m, ensure_ascii=False) for m in session)


async def test_tool_intermediate_reasoning_stripped(tmp_path, monkeypatch):
    """工具调用轮的中间正文若只有思维链，既不发送也不写入历史"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content="<thinking>先查一下时间</thinking>",
                tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(
                content="<thinking>算好了</thinking>现在是 UTC 时间"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("现在几点了", user_id=ROOT_QQ))
        await h.settle()

        # 只发送最终回复，中间思维链不发出
        h.assert_api("send_private_msg").times(1)
        h.assert_api("send_private_msg").with_text("现在是 UTC 时间")

        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert all("<think" not in json.dumps(m, ensure_ascii=False) for m in session)
        assistant_entries = [m for m in session if m["role"] == "assistant"]
        assert assistant_entries[0]["content"] is None  # tool_calls 轮已剥离为无正文
        assert assistant_entries[-1]["content"] == "现在是 UTC 时间"
