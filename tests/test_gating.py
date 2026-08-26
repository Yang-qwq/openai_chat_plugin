# -*- coding: utf-8 -*-
"""消息门控测试：IsConfigured 拦截、MustAtBot @校验、命令前缀跳过。

运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message, private_message

from fake_openai import make_client

pytestmark = pytest.mark.asyncio(mode="strict")

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "200200"
SELF_ID = "10001"  # 与 ncatbot_test_config.yaml 的 bot_uin 一致


def _harness() -> PluginTestHarness:
    """构造隔离的插件测试环境"""
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


def _configure(plugin):
    """在内存中开启配置并替换 OpenAI 客户端为 stub（不持久化）"""
    plugin.config["IsConfigured"] = True
    client = make_client()
    plugin._default_client = client
    return client


async def test_unconfigured_blocks_all_chat(tmp_path, monkeypatch):
    """默认 IsConfigured=False 时，群聊/私聊普通消息不触发任何回复"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(group_message("你好", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").not_called()
        h.assert_api("send_private_msg").not_called()


async def test_must_at_bot_filters_group_message(tmp_path, monkeypatch):
    """MustAtBot=True 时未@机器人的群消息不触发对话（私聊不受影响）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin)

        # 未@机器人：不触发
        await h.inject(group_message("你好", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        assert len(client.chat.completions.calls) == 0
        h.assert_api("send_group_msg").not_called()

        # 私聊无需@：正常触发
        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()
        assert len(client.chat.completions.calls) == 1


async def test_at_bot_triggers_group_chat(tmp_path, monkeypatch):
    """@机器人后群消息进入对话流程并收到回复"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin)

        await h.inject(group_message(
            "你好",
            group_id=GROUP_ID,
            user_id=ROOT_QQ,
            raw_message=f"[CQ:at,qq={SELF_ID}] 你好",
            message=[
                {"type": "at", "data": {"qq": SELF_ID}},
                {"type": "text", "data": {"text": " 你好"}},
            ],
        ))
        await h.settle()

        # 模型被调用且收到回复
        assert len(client.chat.completions.calls) == 1
        h.assert_api("send_group_msg").called().with_text("你好，我是AI")

        # 会话已持久化（data.json 存在且包含 assistant 回复）
        data_file = plugin.workspace / "data.json"
        assert data_file.is_file()
        session = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert {"role": "assistant", "content": "你好，我是AI"} in session


async def test_command_prefix_skips_chat(tmp_path, monkeypatch):
    """以 / 开头的消息不进入对话处理（即使已配置）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin)

        await h.inject(private_message("/chat help", user_id=ROOT_QQ))
        await h.settle()
        # 命令由命令处理器响应，不消耗模型调用
        assert len(client.chat.completions.calls) == 0
