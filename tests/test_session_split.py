# -*- coding: utf-8 -*-
"""会话分离文件存储与 v0.5.0 升级迁移测试

覆盖：
- 旧版 data.json 集中存储的会话一次性拆分到 group|user/<id>.json 分离文件
- 迁移幂等（已存在的分离文件不被覆盖）、旧键清理与 data.json 备份
- 全新环境的会话直接写入分离文件（data.json 不再含会话键）
- 预置分离文件能被正常加载并延续对话
- reset / update-prompt all / 自动清理在分离文件存储上工作

运行：python -m pytest tests -v -o "addopts="
"""
import importlib
import json
import sys
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message, private_message

from fake_openai import FakeChoice, FakeMessage, FakeOpenAIClient, FakeResponse

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "100200"
USER_ID = "314159"

GROUP_MESSAGES = [
    {"role": "user", "content": "群里的历史问题"},
    {"role": "assistant", "content": "群里的历史回答"},
]
USER_MESSAGES = [
    {"role": "user", "content": "私聊的历史问题"},
    {"role": "assistant", "content": "私聊的历史回答"},
]


def _harness() -> PluginTestHarness:
    return PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR)


def _configure(plugin, responses):
    """在内存中开启配置并注入带响应脚本的 OpenAI stub（不持久化）"""
    plugin.config["IsConfigured"] = True
    client = FakeOpenAIClient(responses)
    plugin._default_client = client
    return client


def _write_legacy_data_json(root: Path, group_conversations: dict,
                            user_conversations: dict) -> Path:
    """在插件工作区写入旧版（v0.4.x）格式的 data.json。

    :param root: 运行目录（插件工作区为 root/data/OpenAIChatPlugin/）
    :return: 插件工作区路径
    """
    workspace = root / "data" / "OpenAIChatPlugin"
    workspace.mkdir(parents=True, exist_ok=True)
    legacy = {
        "data": {
            "group_conversations": group_conversations,
            "user_conversations": user_conversations,
            "group_preset_names": {GROUP_ID: "default"},
            "user_preset_names": {},
            "group_nolisten": {},
            "scheduled_tasks": {},
        }
    }
    (workspace / "data.json").write_text(
        json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    return workspace


# ----------------------------------------------------------------------
# 升级迁移
# ----------------------------------------------------------------------

async def test_migration_splits_conversations_to_files(tmp_path, monkeypatch):
    """旧版 data.json 中的群/私聊会话被拆分到分离文件，旧键清除并备份"""
    monkeypatch.chdir(tmp_path)
    # 旧数据中混入 int 型会话键（旧版 /chat-admin 曾用 int 写入），验证归一化
    _write_legacy_data_json(
        tmp_path,
        group_conversations={GROUP_ID: GROUP_MESSAGES, 888: [{"role": "user", "content": "int键"}]},
        user_conversations={USER_ID: USER_MESSAGES},
    )

    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # 分离文件已生成且内容与旧数据一致（int 键归一化为 str 文件名）
        group_file = plugin.workspace / "group" / f"{GROUP_ID}.json"
        user_file = plugin.workspace / "user" / f"{USER_ID}.json"
        int_file = plugin.workspace / "group" / "888.json"
        assert json.loads(group_file.read_text(encoding="utf-8")) == GROUP_MESSAGES
        assert json.loads(user_file.read_text(encoding="utf-8")) == USER_MESSAGES
        assert json.loads(int_file.read_text(encoding="utf-8")) == [{"role": "user", "content": "int键"}]

        # data.json 中会话键已删除，preset_names 等其余键保留
        data = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert "group_conversations" not in data["data"]
        assert "user_conversations" not in data["data"]
        assert data["data"]["group_preset_names"] == {GROUP_ID: "default"}

        # 迁移前已备份 data.json
        backups = list(plugin.workspace.glob("data.json.bak.*"))
        assert len(backups) == 1

        # store 能读到迁移后的历史（沿用旧字典名调用）
        assert plugin._sessions.load("group_conversations", GROUP_ID) == GROUP_MESSAGES
        assert plugin._sessions.load("user_conversations", USER_ID) == USER_MESSAGES


async def test_migration_is_idempotent(tmp_path, monkeypatch):
    """第二次加载不再执行迁移（无新增备份、文件内容不变）"""
    monkeypatch.chdir(tmp_path)
    _write_legacy_data_json(tmp_path, {GROUP_ID: GROUP_MESSAGES}, {USER_ID: USER_MESSAGES})

    for _ in range(2):
        async with _harness() as h:
            plugin = h.get_plugin(PLUGIN_NAME)

    # 仅第一次加载产生了备份
    assert len(list(plugin.workspace.glob("data.json.bak.*"))) == 1
    group_file = plugin.workspace / "group" / f"{GROUP_ID}.json"
    assert json.loads(group_file.read_text(encoding="utf-8")) == GROUP_MESSAGES


async def test_migration_does_not_overwrite_existing_file(tmp_path, monkeypatch):
    """分离文件已存在时跳过该会话（不覆盖新数据），旧键仍清除"""
    monkeypatch.chdir(tmp_path)
    workspace = _write_legacy_data_json(
        tmp_path, {GROUP_ID: GROUP_MESSAGES}, {USER_ID: USER_MESSAGES})
    # 模拟「该会话已按新格式使用过」：分离文件中是更新的内容
    (workspace / "group").mkdir(parents=True, exist_ok=True)
    new_messages = [{"role": "user", "content": "新格式的历史"}]
    (workspace / "group" / f"{GROUP_ID}.json").write_text(
        json.dumps(new_messages, ensure_ascii=False), encoding="utf-8")

    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # 已存在的文件未被旧数据覆盖
        group_file = plugin.workspace / "group" / f"{GROUP_ID}.json"
        assert json.loads(group_file.read_text(encoding="utf-8")) == new_messages
        # 未覆盖的会话仍计入成功，旧键照常清除
        data = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert "group_conversations" not in data["data"]
        # 只有 USER 会话是从旧数据迁出的
        assert json.loads((plugin.workspace / "user" / f"{USER_ID}.json").read_text(encoding="utf-8")) == USER_MESSAGES


def _get_session_store_module():
    """获取插件包内的 session_store 模块（与 harness 加载的为同一模块对象）"""
    module = sys.modules.get("OpenAIChatPlugin.session_store")
    if module is not None:
        return module
    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        return importlib.import_module("OpenAIChatPlugin.session_store")
    finally:
        sys.path.pop(0)


async def test_migration_failure_keeps_legacy_keys(tmp_path, monkeypatch):
    """迁移写入失败时，旧键保留在 data.json 等待下次启动重试"""
    monkeypatch.chdir(tmp_path)
    _write_legacy_data_json(tmp_path, {GROUP_ID: GROUP_MESSAGES}, {USER_ID: USER_MESSAGES})

    # 让 store 写盘失败（磁盘异常等），作用于插件实际使用的 SessionStore 类。
    # 用独立 MonkeyPatch 注入：撤销时不影响 monkeypatch.chdir 的沙箱隔离
    session_store_module = _get_session_store_module()

    def failing_replace(self, conv_type, session_id, messages):
        raise OSError("模拟磁盘故障")

    failure_patch = pytest.MonkeyPatch()
    failure_patch.setattr(session_store_module.SessionStore, "replace", failing_replace)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # 旧会话键保留（下次启动可重试）
        assert plugin.data["data"]["group_conversations"] == {GROUP_ID: GROUP_MESSAGES}
        assert plugin.data["data"]["user_conversations"] == {USER_ID: USER_MESSAGES}
        # 备份仍生成
        assert list(plugin.workspace.glob("data.json.bak.*"))

        # 捕获绝对路径：会话退出（卸载写盘）后 monkeypatch 仍需保持 chdir
        workspace = plugin.workspace.resolve()

    # 恢复写盘能力（仅撤销故障注入，chdir 沙箱保持）
    failure_patch.undo()

    # 会话退出时卸载流程写回 data.json，旧键必须仍然保留
    legacy_after_failure = json.loads(
        (workspace / "data.json").read_text(encoding="utf-8"))
    assert legacy_after_failure["data"]["group_conversations"] == {GROUP_ID: GROUP_MESSAGES}
    assert legacy_after_failure["data"]["user_conversations"] == {USER_ID: USER_MESSAGES}

    # 再次加载，完成迁移
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        group_file = plugin.workspace / "group" / f"{GROUP_ID}.json"
        assert json.loads(group_file.read_text(encoding="utf-8")) == GROUP_MESSAGES
        data = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert "group_conversations" not in data["data"]


# ----------------------------------------------------------------------
# 运行时：分离文件存储
# ----------------------------------------------------------------------

async def test_new_conversation_written_to_split_file(tmp_path, monkeypatch):
    """全新环境对话，会话写入分离文件而非 data.json"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("你好，我是AI"), "stop"))])

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        session_file = plugin.workspace / "user" / f"{ROOT_QQ}.json"
        assert session_file.is_file()
        session = json.loads(session_file.read_text(encoding="utf-8"))
        assert [m["role"] for m in session] == ["user", "assistant"]

        # data.json 不含会话键
        data = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert "user_conversations" not in data["data"]


async def test_corrupt_session_file_reinitializes(tmp_path, monkeypatch):
    """会话文件损坏时按默认预设重建，而不是永久报错不可用"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("你好，我是AI"), "stop"))])

        session_file = plugin.workspace / "user" / f"{ROOT_QQ}.json"
        session_file.parent.mkdir(parents=True, exist_ok=True)
        session_file.write_text("{ 这不是合法 JSON", encoding="utf-8")

        await h.inject(private_message("你好", user_id=ROOT_QQ))
        await h.settle()

        # 正常回复，且损坏文件被重建为合法会话
        h.assert_api("send_private_msg").with_text("你好，我是AI")
        session = json.loads(session_file.read_text(encoding="utf-8"))
        assert [m["role"] for m in session] == ["user", "assistant"]


async def test_existing_split_file_session_continues(tmp_path, monkeypatch):
    """预置分离文件中的历史被加载并参与后续对话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin._sessions.replace("user_conversations", ROOT_QQ, list(USER_MESSAGES))
        client = _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("继续回答"), "stop"))])

        await h.inject(private_message("继续", user_id=ROOT_QQ))
        await h.settle()

        # 模型收到了预置的历史上下文
        sent_messages = client.chat.completions.calls[0]["messages"]
        assert {"role": "user", "content": "私聊的历史问题"} in sent_messages
        assert {"role": "assistant", "content": "私聊的历史回答"} in sent_messages

        # 分离文件追加了本轮对话
        session_file = plugin.workspace / "user" / f"{ROOT_QQ}.json"
        session = json.loads(session_file.read_text(encoding="utf-8"))
        assert [m["role"] for m in session] == ["user", "assistant", "user", "assistant"]


async def test_reset_command_resets_split_file(tmp_path, monkeypatch):
    """/chat reset 把分离文件重置为预设初始消息"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin._sessions.replace("user_conversations", ROOT_QQ, list(USER_MESSAGES))

        await h.inject(private_message("/chat reset", user_id=ROOT_QQ))
        await h.settle()

        session = plugin._sessions.load("user_conversations", ROOT_QQ)
        assert session == []  # 默认预设 prompt.md 为空，初始消息为空列表


async def test_update_prompt_all_covers_file_backed_sessions(tmp_path, monkeypatch):
    """/chat-admin update-prompt all 能覆盖仅存在于分离文件的会话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        # 默认预设写入有效 system 提示词
        (plugin.workspace / "presets" / "default" / "prompt.md").write_text(
            "你是全新提示词", encoding="utf-8")
        # 会话只存在于分离文件（未经本轮对话加载）
        plugin._sessions.replace("group_conversations", GROUP_ID, list(GROUP_MESSAGES))

        await h.inject(group_message("/chat-admin update-prompt all",
                                     group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()

        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert session[0] == {"role": "system", "content": "你是全新提示词"}
        assert len(session) == len(GROUP_MESSAGES) + 1  # 其余历史保留


async def test_auto_cleanup_scans_file_backed_sessions(tmp_path, monkeypatch):
    """自动清理能枚举仅存在于磁盘的会话文件并执行压缩"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = False
        plugin.config["AllowAccessMemory"] = False
        plugin.config["EnableConversationCompression"] = True
        plugin.config["CompressionThresholdBytes"] = 100
        plugin.config["CompressionKeepRecentBytes"] = 20
        _configure(plugin, [FakeResponse(FakeChoice(FakeMessage("压缩摘要"), "stop"))])

        # 大会话只存在于分离文件（未经任何活跃会话加载）
        plugin._sessions.replace("group_conversations", GROUP_ID, [
            {"role": "user", "content": "x" * 50} for _ in range(5)
        ])

        result = await plugin._compress_conversation("group_conversations", GROUP_ID)
        assert result == "compressed"
        session = plugin._sessions.load("group_conversations", GROUP_ID)
        assert len(session) < 5
        assert any("[历史摘要]" in str(m.get("content", "")) for m in session)
