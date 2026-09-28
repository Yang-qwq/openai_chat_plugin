# -*- coding: utf-8 -*-
"""与 NcatBotTRPG 跑团插件的协调测试

跑团进行中的会话由 NcatBotTRPG 接管，OpenAIChatPlugin 应让出处理权，
避免 @机器人 时两个插件重复回复；跑团未进行时 OpenAI 对话不受影响。

运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message

from fake_openai import make_client

pytestmark = pytest.mark.asyncio(mode="strict")

PLUGINS_DIR = Path(__file__).resolve().parents[2]
OPENAI_PLUGIN = "OpenAIChatPlugin"
TRPG_PLUGIN = "NcatBotTRPG"
ROOT_QQ = "123456"  # ncatbot_test_config.yaml 的 root，用于群内开团
GROUP_ID = "200200"
SELF_ID = "10001"


def _harness() -> PluginTestHarness:
    """同时加载 OpenAIChatPlugin 与 NcatBotTRPG"""
    return PluginTestHarness(
        plugin_names=[OPENAI_PLUGIN, TRPG_PLUGIN],
        plugins_dir=PLUGINS_DIR,
    )


def _at_bot_message():
    """构造 @机器人 的群消息事件"""
    return group_message(
        "你好",
        group_id=GROUP_ID,
        user_id=ROOT_QQ,
        raw_message=f"[CQ:at,qq={SELF_ID}] 你好",
        message=[
            {"type": "at", "data": {"qq": SELF_ID}},
            {"type": "text", "data": {"text": " 你好"}},
        ],
    )


async def _activate_trpg(h):
    """开启本群的 TRPG 团，并替换其 LLM 客户端为 stub（避免真实网络）"""
    trpg = h.get_plugin(TRPG_PLUGIN)
    trpg.config["IsConfigured"] = True
    trpg.llm._client = make_client("测试主持回复")
    await h.inject(group_message("/trpg start", group_id=GROUP_ID, user_id=ROOT_QQ))
    await h.settle()
    return trpg


async def test_group_at_bot_skipped_when_trpg_active(tmp_path, monkeypatch):
    """TRPG 团进行中：OpenAIChatPlugin 不响应群 @机器人，交由跑团插件接管"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        openai = h.get_plugin(OPENAI_PLUGIN)
        openai.config["IsConfigured"] = True
        client = make_client("你好，我是AI")
        openai._default_client = client

        await _activate_trpg(h)
        h.reset_api()

        await h.inject(_at_bot_message())
        await h.settle()

        # OpenAI 对话未被触发，跑团插件接管并回复
        assert len(client.chat.completions.calls) == 0
        h.assert_api("send_group_msg").with_text("测试主持回复")


async def test_group_at_bot_works_when_trpg_inactive(tmp_path, monkeypatch):
    """TRPG 未开团：OpenAIChatPlugin 正常响应群 @机器人"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        openai = h.get_plugin(OPENAI_PLUGIN)
        openai.config["IsConfigured"] = True
        client = make_client("你好，我是AI")
        openai._default_client = client

        # NcatBotTRPG 已加载但未开团
        await h.inject(_at_bot_message())
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        h.assert_api("send_group_msg").with_text("你好，我是AI")


async def test_group_at_bot_works_after_trpg_stopped(tmp_path, monkeypatch):
    """TRPG 结束团后，OpenAIChatPlugin 恢复响应群 @机器人"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        openai = h.get_plugin(OPENAI_PLUGIN)
        openai.config["IsConfigured"] = True
        client = make_client("你好，我是AI")
        openai._default_client = client

        await _activate_trpg(h)
        await h.inject(group_message("/trpg stop", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.reset_api()

        await h.inject(_at_bot_message())
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        h.assert_api("send_group_msg").with_text("你好，我是AI")
