# -*- coding: utf-8 -*-
"""群聊随机插话测试

覆盖：概率命中绕过 MustAtBot、模型回 NO_REPLY/空正文保持沉默、群级 nolisten、
冷却时间、插话提示固定 user 角色（避免双 system 被严格网关拒绝）。

运行：python -m pytest tests -v -o "addopts="
"""
import json
import random
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
    make_rate_limit_error,
)

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "200200"
MEMBER_QQ = "99999"
SELF_ID = "10001"


def _harness() -> PluginTestHarness:
    return PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR)


def _configure(plugin, replies, *, chance=1.0):
    """开启随机插话并注入 stub 客户端"""
    plugin.config["IsConfigured"] = True
    plugin.config["EnableRandomReplyGroup"] = True
    plugin.config["GroupRandomReplyChance"] = chance
    client = FakeOpenAIClient([
        reply if isinstance(reply, BaseException)
        else FakeResponse(FakeChoice(FakeMessage(reply), "stop"))
        for reply in replies
    ])
    plugin._default_client = client
    return client


def _force_random_hit(monkeypatch):
    """让概率判定恒为命中"""
    monkeypatch.setattr(random, "random", lambda: 0.0)


def _group_text(text="今天天气不错"):
    return group_message(text, group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID)


async def test_random_reply_bypasses_must_at_bot(tmp_path, monkeypatch):
    """命中概率时，未 @机器人 的群消息也会参与对话"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, ["确实，今天适合出门"])

        await h.inject(_group_text())
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        h.assert_api("send_group_msg").with_text("确实，今天适合出门")

        session = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert session[-1] == {"role": "assistant", "content": "确实，今天适合出门"}


async def test_random_reply_disabled_keeps_must_at_bot(tmp_path, monkeypatch):
    """未开启随机插话时，未 @机器人 的群消息不触发对话"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage("不该出现"), "stop"))])
        plugin._default_client = client

        await h.inject(_group_text())
        await h.settle()

        assert len(client.chat.completions.calls) == 0
        h.assert_api("send_group_msg").not_called()
        assert GROUP_ID not in plugin.data["data"]["group_conversations"]


async def test_nolisten_closes_random_reply(tmp_path, monkeypatch):
    """/chat nolisten on 后本群不再随机插话，且开关持久化"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, ["不该出现"])
        assert plugin.data["data"]["group_nolisten"] == {}  # 数据键已初始化

        await h.inject(group_message(
            "/chat nolisten on", group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID))
        await h.settle()

        h.assert_api("send_group_msg").with_text("已关闭本群（200200）的随机插话")
        assert plugin.data["data"]["group_nolisten"][GROUP_ID] is True

        # 持久化到 data.json
        saved = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert saved["data"]["group_nolisten"][GROUP_ID] is True

        h.reset_api()
        await h.inject(_group_text())
        await h.settle()

        assert len(client.chat.completions.calls) == 0
        h.assert_api("send_group_msg").not_called()


async def test_nolisten_toggle_and_query(tmp_path, monkeypatch):
    """/chat nolisten 无参查询状态；off 重新开启随机插话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True

        await h.inject(group_message(
            "/chat nolisten", group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID))
        await h.settle()
        h.assert_api("send_group_msg").with_text("本群随机插话当前已开启（用法：/chat nolisten on|off）")

        await h.inject(group_message(
            "/chat nolisten on", group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID))
        await h.settle()
        await h.inject(group_message(
            "/chat nolisten", group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID))
        await h.settle()
        h.assert_api("send_group_msg").with_text("本群随机插话当前已关闭（用法：/chat nolisten on|off）")

        await h.inject(group_message(
            "/chat nolisten off", group_id=GROUP_ID, user_id=MEMBER_QQ, self_id=SELF_ID))
        await h.settle()
        assert plugin.data["data"]["group_nolisten"][GROUP_ID] is False


@pytest.mark.parametrize("silent_reply", ["NO_REPLY", "`NO_REPLY`", "NO_REPLY.", "no-reply", ""])
async def test_silent_reply_does_not_send(tmp_path, monkeypatch, silent_reply):
    """模型回 NO_REPLY 哨兵或空正文时保持沉默：不发送、不写 assistant 历史"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, [silent_reply])

        await h.inject(_group_text())
        await h.settle()

        h.assert_api("send_group_msg").not_called()
        session = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert [m["role"] for m in session] == ["user"]


async def test_random_reply_cooldown(tmp_path, monkeypatch):
    """同一群冷却期内不会连续随机插话"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, ["第一次", "第二次"])

        await h.inject(_group_text("第一条消息"))
        await h.settle()
        await h.inject(_group_text("第二条消息"))
        await h.settle()

        assert len(client.chat.completions.calls) == 1
        h.assert_api("send_group_msg").times(1)


async def test_random_reply_hint_always_user(tmp_path, monkeypatch):
    """随机插话的临时提示固定为 user 角色，保证预设是唯一的 system

    部分网关（如 AMD Radeon、智谱 GLM）只接受位于开头的单个 system，
    第二条 system 会被 400 拒绝（'System message must be at the beginning.'），
    因此提示角色不再可配置。提示紧邻本次用户消息，不写回会话历史。
    """
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, ["好的"])
        (plugin.workspace / "presets" / "default" / "prompt.md").write_text(
            "你是一个群友", encoding="utf-8")

        await h.inject(_group_text())
        await h.settle()

        messages = client.chat.completions.calls[0]["messages"]
        assert [m["role"] for m in messages] == ["system", "user", "user"]
        hint = [m for m in messages if "NO_REPLY" in str(m["content"])]
        assert len(hint) == 1
        assert hint[0]["role"] == "user"
        # 提示紧贴本次用户消息，而不是插在 system 之后
        assert messages[-2] is hint[0]


async def test_random_reply_hint_inserted_before_latest_message(tmp_path, monkeypatch):
    """已有历史时，提示插在本次用户消息之前，且不写回会话历史"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        client = _configure(plugin, ["第一次插话", "第二次插话"])
        (plugin.workspace / "presets" / "default" / "prompt.md").write_text(
            "你是一个群友", encoding="utf-8")

        await h.inject(_group_text("第一条消息"))
        await h.settle()
        # 绕过 30s 冷却，模拟一段时间后的第二条消息
        plugin._last_random_reply_ts.clear()
        await h.inject(_group_text("第二条消息"))
        await h.settle()

        messages = client.chat.completions.calls[1]["messages"]
        assert [m["role"] for m in messages] == [
            "system", "user", "assistant", "user", "user"]
        assert messages[-1]["content"] == "第二条消息"
        assert "NO_REPLY" in messages[-2]["content"]
        # 提示只存在于本次请求副本，不写回持久化历史
        session = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert [m["role"] for m in session] == [
            "system", "user", "assistant", "user", "assistant"]
        assert all("NO_REPLY" not in str(m.get("content")) for m in session)


async def test_random_reply_rate_limit_stays_silent(tmp_path, monkeypatch):
    """随机插话遇到持续限流（429，重试耗尽）时静默放弃，不向群里发错误提示"""
    monkeypatch.chdir(tmp_path)
    _force_random_hit(monkeypatch)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, [make_rate_limit_error()])

        await h.inject(_group_text())
        await h.settle()

        # 不发送任何消息（既不是 AI 回复，也不是「插件内部错误」）
        h.assert_api("send_group_msg").not_called()
        session = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert [m["role"] for m in session] == ["user"]
