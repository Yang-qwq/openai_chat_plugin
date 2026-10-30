# -*- coding: utf-8 -*-
"""会话压缩功能测试

覆盖：阈值触发、记忆压缩路径、摘要压缩路径、未达阈值不触发、
压缩失败降级、大小计算等核心功能。

运行：python -m pytest tests -v -o "addopts="
"""
import asyncio
import json
import os
import time
from pathlib import Path
from unittest.mock import patch

import pytest
from ncatbot.testing import PluginTestHarness

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
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


def _make_client(reply="压缩成功", intermediate=None):
    """创建单轮直答客户端"""
    responses = [FakeResponse(FakeChoice(FakeMessage(content=reply), "stop"))]
    return FakeOpenAIClient(responses)


def _numbered_conversation(count=6, system="你是群里的猫娘"):
    """构造带 system 提示与可辨识序号的消息序列（用于验证保留窗口方向）"""
    conv = [{"role": "system", "content": system}]
    conv += [{"role": "user" if i % 2 == 0 else "assistant", "content": f"MSG-{i}" + "x" * 40}
             for i in range(count)]
    return conv


async def test_compression_disabled_by_default(tmp_path, monkeypatch):
    """默认禁用压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        # 未配置压缩开关
        assert "EnableConversationCompression" in plugin.config
        assert plugin.config["EnableConversationCompression"] is False
        # 即使自动清理开启也不应触发压缩
        plugin.config["EnableAutoCleanup"] = True
        # 模拟大量消息
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": f"消息{i}"} for i in range(10)
        ])
        # 手动调用压缩 - 应被跳过
        result = await plugin._compress_conversation("group_conversations", GROUP_ID)
        assert result == "skipped"


async def test_compression_triggered_by_bytes(tmp_path, monkeypatch):
    """字节数超过阈值触发压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100  # 很小的阈值
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = False  # 禁用记忆工具
        plugin.config["AllowAccessMemory"] = False
        plugin._default_client = _make_client("压缩摘要")

        # 构造足够大的会话
        large_messages = [{"role": "user", "content": "x" * 50} for _ in range(5)]
        plugin._sessions.replace("group_conversations", GROUP_ID, large_messages)

        # 执行压缩
        result = await plugin._compress_conversation("group_conversations", GROUP_ID)
        assert result == "compressed"

        # 验证会话变小了
        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert len(session) < 5  # 压缩后消息减少
        # 应包含一条摘要消息
        assert any("[历史摘要]" in str(m.get("content", "")) for m in session)


async def test_compression_uses_memory_when_available(tmp_path, monkeypatch):
    """记忆工具可用时使用记忆压缩路径"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100  # 很小的阈值
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowAccessMemory"] = True
        # 需要配置 ApiKey 等基础配置
        plugin.config["ApiKey"] = "sk-test"
        plugin.config["Model"] = "openai/gpt-4o-mini"
        # 设置 mock 客户端（压缩调用会生成摘要）
        mock_client = _make_client("压缩摘要")
        plugin._default_client = mock_client

        # 创建足够长的会话消息（总大小超过阈值）
        # 每条消息约 30 字节，需要至少 4 条
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "x" * 30} for _ in range(5)
        ])

        # Mock access_memory 工具和 asyncio.to_thread（避免真实 API 调用）。
        # 注意：harness 以包名 OpenAIChatPlugin 导入插件，patch 目标不能用
        # plugins.OpenAIChatPlugin.* 的路径（该模块不存在）
        memory_calls = []
        async def mock_to_thread(func, *args):
            # 模拟生成摘要 - 返回一个 FakeResponse 对象
            return FakeResponse(FakeChoice(FakeMessage(content="压缩摘要"), "stop"))

        with patch("asyncio.to_thread", side_effect=mock_to_thread):
            def mock_access_memory(workspace, action, content=None, **kwargs):
                memory_calls.append({
                    "workspace": workspace,
                    "action": action,
                    "content": content,
                    "kwargs": kwargs,
                })
                return json.dumps({"status": "success", "message": "success"})

            with patch("OpenAIChatPlugin.tools.access_memory", side_effect=mock_access_memory):
                # 执行压缩
                result = await plugin._compress_conversation("group_conversations", GROUP_ID)
                assert result == "compressed"

                # 验证调用了记忆工具
                assert len(memory_calls) == 1
                assert memory_calls[0]["action"] == "add"
                # 内容应包含压缩摘要
                assert "[会话压缩摘要]" in memory_calls[0]["content"]
                # from_group 应为群 ID
                assert memory_calls[0]["kwargs"]["from_group"] == int(GROUP_ID)

        # 验证原始消息被移除
        session = plugin._sessions.load("group_conversations", GROUP_ID)
        # 消息数量减少，不保留原始的 5 条
        assert len(session) < 5


async def test_compression_size_calculation(tmp_path, monkeypatch):
    """会话大小计算测试"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # 测试字节计算
        messages = [
            {"role": "user", "content": "hello"},
            {"role": "assistant", "content": "你好"},
        ]
        size_bytes = plugin._calculate_conversation_size(messages, method="bytes")
        assert size_bytes > 0
        # 长度应该大致合理
        assert size_bytes < 200  # 简单消息不会太大

        # 测试 token 估算
        tokens = plugin._calculate_conversation_size(messages, method="tokens")
        assert tokens > 0
        # token 数应该与消息长度正相关
        assert tokens < 50


async def test_compression_keep_recent_bytes(tmp_path, monkeypatch):
    """保留最近消息的大小限制"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 50  # 保留 50 字节
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False
        plugin._default_client = _make_client("压缩摘要")

        # 创建消息：每条消息约 50 字节
        messages = [{"role": "user", "content": "x" * 20} for _ in range(5)]
        plugin._sessions.replace("group_conversations", GROUP_ID, messages)

        await plugin._compress_conversation("group_conversations", GROUP_ID)

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        # 至少保留最后一条消息（最小保留）
        assert len(session) >= 1
        # 如果保留恰好 50 字节，最后一条消息约 50 字节，应该保留
        # 由于四舍五入，可能保留更多
        assert len(session) <= 2  # 不太可能保留全部 5 条

        # 摘要插在最前面，最近的原文消息排在它后面（保留的是尾部而不是头部）
        assert "[历史摘要]" in str(session[0].get("content", ""))
        assert session[-1]["content"] == "x" * 20


async def test_compression_failure_handling(tmp_path, monkeypatch):
    """压缩失败时的处理"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False

        # 创建消息
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "消息{i}"} for i in range(5)
        ])

        # Mock 生成摘要的 API 调用出错
        with patch.object(plugin, "_default_client") as mock_client:
            mock_client.chat.completions.create.side_effect = Exception("API 错误")

            result = await plugin._compress_conversation("group_conversations", GROUP_ID)
            assert result == "failed"

            # 会话 unchanged (因为失败不修改)
            session = plugin._sessions.load("group_conversations", GROUP_ID)
            assert len(session) == 5


async def test_compression_small_session_skipped(tmp_path, monkeypatch):
    """小会话不压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 1000  # 大阈值
        plugin.config["CompressionKeepRecentBytes"] = 100

        # 很小的会话
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "hi"},
            {"role": "assistant", "content": "hello"},
        ])

        result = await plugin._compress_conversation("group_conversations", GROUP_ID)
        assert result == "skipped"
        # 会话 unchanged
        assert len(plugin._sessions.load("group_conversations", GROUP_ID)) == 2


async def test_compression_with_image_content(tmp_path, monkeypatch):
    """包含图片的会话压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 100
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False
        plugin._default_client = _make_client("用户分享了一张图片")

        # 包含图片消息的会话（需超过 3 条消息的压缩下限）
        messages = [
            {"role": "user", "content": [
                {"type": "text", "text": "这是一张图片"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
            ]},
            {"role": "assistant", "content": "我看到了图片"},
            {"role": "user", "content": "x" * 50},
            {"role": "user", "content": "y" * 50},
        ]
        plugin._sessions.replace("group_conversations", GROUP_ID, messages)

        await plugin._compress_conversation("group_conversations", GROUP_ID)

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        # 压缩后的摘要中的 content 应该是字符串，不包含图片 data URL
        summary_indices = [i for i, m in enumerate(session) if "[历史摘要]" in str(m.get("content", ""))]
        assert len(summary_indices) >= 1
        # 摘要内容应该是纯文本，不包含 base64
        assert "base64" not in session[summary_indices[0]]["content"]


async def test_auto_cleanup_loop_sleeps(tmp_path, monkeypatch):
    """自动清理循环的休眠测试"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableAutoCleanup"] = True
        plugin.config["AutoCleanupInterval"] = 0.1  # 很短的间隔

        # Mock _auto_cleanup_all
        cleanup_calls = []
        async def mock_cleanup():
            cleanup_calls.append(True)

        with patch.object(plugin, "_auto_cleanup_all", side_effect=mock_cleanup):
            # 循环为 while True，用任务方式运行一段时间后取消（wait_for 会因
            # 循环永不退出而超时）
            task = asyncio.create_task(plugin._auto_cleanup_loop())
            await asyncio.sleep(0.35)
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

            # 0.1s 间隔下 0.35s 应至少触发 2 次清理
            assert len(cleanup_calls) >= 2


async def test_auto_cleanup_skips_active_session(tmp_path, monkeypatch):
    """刚活跃的会话（闲置不足阈值）不被自动压缩，不为活跃会话消耗 token"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False
        plugin._default_client = _make_client("压缩摘要")

        # 超过阈值的大会话，但刚刚才落库（活跃）
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "x" * 50} for _ in range(5)
        ])
        calls_before = len(plugin._default_client.chat.completions.calls)

        await plugin._auto_cleanup_all()

        # 未发起摘要请求，会话原样保留
        assert len(plugin._default_client.chat.completions.calls) == calls_before
        assert len(plugin._sessions.load("group_conversations", GROUP_ID)) == 5


async def test_auto_cleanup_compresses_idle_session(tmp_path, monkeypatch):
    """闲置超过 AutoCleanupIdleSeconds（默认 8 小时）的会话被自动压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False
        plugin._default_client = _make_client("压缩摘要")

        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "x" * 50} for _ in range(5)
        ])
        # 把会话文件 mtime 回拨到 9 小时前（超过默认 8 小时闲置门槛，模拟长期闲置）
        session_file = plugin.workspace / "group" / f"{GROUP_ID}.json"
        old = time.time() - 9 * 3600
        os.utime(session_file, (old, old))

        await plugin._auto_cleanup_all()

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert len(session) < 5
        assert any("[历史摘要]" in str(m.get("content", "")) for m in session)


# ----------------------------------------------------------------------
# 会话压缩：保留窗口方向 / system 提示 / 摘要位置（v0.5.0 回归）
# ----------------------------------------------------------------------

def _enable_summary_compression(plugin, keep_recent_bytes):
    plugin.config["IsConfigured"] = True
    plugin.config["EnableConversationCompression"] = True
    plugin.config["CompressionThresholdBytes"] = 100
    plugin.config["CompressionKeepRecentBytes"] = keep_recent_bytes
    plugin.config["EnableBuiltinFunctionCalling"] = False
    plugin.config["AllowAccessMemory"] = False
    plugin._default_client = _make_client("压缩摘要")
    return plugin


async def test_compression_keeps_newest_messages(tmp_path, monkeypatch):
    """压缩保留的是**最近**的消息，而不是最早的消息"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = _enable_summary_compression(h.get_plugin(PLUGIN_NAME), keep_recent_bytes=150)
        plugin._sessions.replace("group_conversations", GROUP_ID, _numbered_conversation(6))

        result = await plugin._compress_conversation("group_conversations", GROUP_ID)
        assert result == "compressed"

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        contents = [str(m.get("content", "")) for m in session]
        # 最新几条必须仍在（MSG-5 是最后一条）
        assert any("MSG-5" in c for c in contents), contents
        # 最早的消息应被压缩掉
        assert not any("MSG-0x" in c for c in contents), contents
        # 保留窗口是连续尾部：被保留的消息序号必须大于被删除的
        kept_numbers = [int("".join(filter(str.isdigit, c.split("x")[0]))) for c in contents if "MSG-" in c]
        assert kept_numbers == sorted(kept_numbers)
        assert kept_numbers[-1] == 5


async def test_compression_preserves_system_prompt(tmp_path, monkeypatch):
    """压缩不动首条 system 提示词（预设人格必须保留）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = _enable_summary_compression(h.get_plugin(PLUGIN_NAME), keep_recent_bytes=150)
        plugin._sessions.replace("group_conversations", GROUP_ID, _numbered_conversation(6))

        await plugin._compress_conversation("group_conversations", GROUP_ID)

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert session[0] == {"role": "system", "content": "你是群里的猫娘"}


async def test_compression_summary_inserted_after_system(tmp_path, monkeypatch):
    """摘要插在 system 之后（而不是会话末尾），且模型请求里顺序正确"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = _enable_summary_compression(h.get_plugin(PLUGIN_NAME), keep_recent_bytes=150)
        plugin._sessions.replace("group_conversations", GROUP_ID, _numbered_conversation(6))

        await plugin._compress_conversation("group_conversations", GROUP_ID)

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert session[0]["role"] == "system"
        assert session[1]["role"] == "user"
        assert session[1]["content"].startswith("[历史摘要]")
        # 摘要不是最后一条，最近的真实消息排在它后面
        assert not str(session[-1]["content"]).startswith("[历史摘要]")


async def test_compression_memory_write_failure_keeps_history(tmp_path, monkeypatch):
    """记忆写入失败（返回 error payload）时不删除会话历史"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 20
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowAccessMemory"] = True
        plugin._default_client = _make_client("压缩摘要")

        original = [{"role": "user", "content": "x" * 50} for _ in range(5)]
        plugin._sessions.replace("group_conversations", GROUP_ID, original)

        def failing_access_memory(*args, **kwargs):
            return json.dumps({"status": "error", "message": "memory.json 写入失败"})

        with patch("OpenAIChatPlugin.tools.access_memory", side_effect=failing_access_memory):
            result = await plugin._compress_conversation("group_conversations", GROUP_ID)

        assert result == "failed"
        assert plugin._sessions.load("group_conversations", GROUP_ID) == original


async def test_compression_summary_input_covers_whole_history(tmp_path, monkeypatch):
    """超预算时摘要输入等距抽样，覆盖整段历史而不是只取开头"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        messages = [{"role": "user", "content": f"MSG-{i}" + "y" * 900} for i in range(60)]
        text = plugin._build_summary_input(messages)

        assert len(text) <= 12000
        assert "MSG-0" in text          # 覆盖开头
        assert "MSG-59" in text         # 覆盖结尾（旧实现只截前 4000 字符时会丢）


async def test_compression_image_part_rendered_as_placeholder(tmp_path, monkeypatch):
    """摘要输入里图片渲染成 [图片] 占位，且不含 base64"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        text = plugin._build_summary_input([
            {"role": "user", "content": [
                {"type": "text", "text": "看图"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,abc123"}},
            ]},
        ])
        assert "[图片]" in text
        assert "base64" not in text


async def test_compression_requires_only_compression_switch(tmp_path, monkeypatch):
    """仅开启 EnableConversationCompression 也会启动后台压缩（无需 EnableAutoCleanup）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableConversationCompression"] = True
        plugin.config["EnableAutoCleanup"] = False
        assert plugin._cleanup_enabled() is True

        plugin.config["EnableConversationCompression"] = False
        assert plugin._cleanup_enabled() is False
        plugin.config["EnableAutoCleanup"] = True
        assert plugin._cleanup_enabled() is True


async def test_cleanup_interval_falls_back_on_invalid_value(tmp_path, monkeypatch):
    """AutoCleanupInterval 为 0/负值/非法值时回退默认（30 分钟），避免忙等扫描"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        for bad in (0, -5, None, "abc"):
            plugin.config["AutoCleanupInterval"] = bad
            assert plugin._cleanup_interval_seconds() == 1800.0
        plugin.config["AutoCleanupInterval"] = 0.1
        assert plugin._cleanup_interval_seconds() == 0.1


async def test_cleanup_task_cancelled_on_close(tmp_path, monkeypatch):
    """插件卸载时取消后台清理任务，避免热重载后留下孤儿任务"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["EnableConversationCompression"] = True
        plugin.config["AutoCleanupInterval"] = 0.5

        task = asyncio.create_task(plugin._auto_cleanup_loop())
        plugin._cleanup_task = task
        await asyncio.sleep(0)
        assert not task.done()

        await plugin.on_close()
        assert task.cancelled()
        assert plugin._cleanup_task is None
