# -*- coding: utf-8 -*-
"""推理模型思维链剥离测试

模型把思维链以 … 等标签混入正文时，回复与会话历史都不得包含思维链。

运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness, extract_text
from ncatbot.testing.factories.qq import private_message

from fake_openai import FakeChoice, FakeMessage, FakeOpenAIClient, FakeResponse

pytestmark = pytest.mark.asyncio(mode="strict")

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
