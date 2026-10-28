# -*- coding: utf-8 -*-
"""纯文本模型（不支持多模态 content）兼容测试

覆盖智谱 GLM 400 code 1210（``messages.content.type 参数非法，取值范围 ['text']``）：
历史里存在 ``image_url`` 时自动降级纯文本重试、后续轮次提前压平、
调用方不修改会话历史、无关 400 不误判。

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
    make_bad_request_error,
)

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"

# 智谱 GLM 实际返回的 400 文案
GLM_TEXT_ONLY_MESSAGE = (
    "Error code: 400 - {'error': {'code': '1210', 'message': "
    "\"messages.content.type 参数非法，取值范围 ['text']\"}}"
)
IMAGE_PART = {'type': 'image_url', 'image_url': {'url': 'data:image/png;base64,AAAA'}}


def _harness() -> PluginTestHarness:
    return PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR)


def _configure(plugin, responses):
    plugin.config["IsConfigured"] = True
    client = FakeOpenAIClient(responses)
    plugin._default_client = client
    return client


def _with_image_history(plugin):
    """伪造“上一轮用户发过图片”的会话历史（图片以多模态 list 持久化）"""
    plugin.data["data"]["user_conversations"][ROOT_QQ] = [
        {
            'role': 'user',
            'content': [
                {'type': 'text', 'text': '看图'},
                IMAGE_PART,
            ],
        },
        {'role': 'assistant', 'content': '收到'},
    ]


def _has_multimodal_content(messages) -> bool:
    return any(isinstance(m.get('content'), list) for m in messages)


async def test_text_only_model_retries_with_plain_text(tmp_path, monkeypatch):
    """历史含 image_url 触发 400 时，自动压平为纯文本重试并记住模型不支持视觉"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _with_image_history(plugin)
        client = _configure(plugin, [
            make_bad_request_error(GLM_TEXT_ONLY_MESSAGE),
            FakeResponse(FakeChoice(FakeMessage("好的，我看看"), "stop")),
        ])

        await h.inject(private_message("还有没有建议", user_id=ROOT_QQ))
        await h.settle()

        # 第一次按原样请求（含 image_url），第二次降级为纯文本
        assert len(client.chat.completions.calls) == 2
        first_messages = client.chat.completions.calls[0]["messages"]
        retry_messages = client.chat.completions.calls[1]["messages"]
        assert _has_multimodal_content(first_messages)
        assert not _has_multimodal_content(retry_messages)
        assert "image_url" not in str(retry_messages)
        assert "[图片]" in str(retry_messages)

        assert plugin._vision_unsupported is True
        h.assert_api("send_private_msg").with_text("好的，我看看")

        # 只降级请求副本，会话历史保留原始多模态消息
        history = plugin.data["data"]["user_conversations"][ROOT_QQ]
        assert isinstance(history[0]["content"], list)


async def test_known_text_only_model_sanitizes_proactively(tmp_path, monkeypatch):
    """已探测到不支持视觉后，后续请求直接压平，不再靠 400 触发重试"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _with_image_history(plugin)
        plugin._vision_unsupported = True
        client = _configure(plugin, [
            FakeResponse(FakeChoice(FakeMessage("继续聊"), "stop")),
        ])

        await h.inject(private_message("在吗", user_id=ROOT_QQ))
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        messages = client.chat.completions.calls[0]["messages"]
        assert not _has_multimodal_content(messages)
        assert "image_url" not in str(messages)
        h.assert_api("send_private_msg").with_text("继续聊")


async def test_unrelated_400_is_not_misidentified(tmp_path, monkeypatch):
    """与 content.type 无关的 400 不应触发视觉降级（仍按内部错误处理）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _with_image_history(plugin)
        client = _configure(plugin, [
            make_bad_request_error("invalid api key", code="1000"),
            FakeResponse(FakeChoice(FakeMessage("不该出现"), "stop")),
        ])

        await h.inject(private_message("在吗", user_id=ROOT_QQ))
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        assert plugin._vision_unsupported is False
        h.assert_api("send_private_msg").with_text("抱歉，插件出现内部错误，请稍后再试")
