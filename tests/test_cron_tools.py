# -*- coding: utf-8 -*-
"""AI 定时任务工具测试

覆盖：工具可见性开关、增删查与持久化、注册失败处理、到点生成并投递、
重启恢复（重新注册）以及未开启时的分派拦截。

运行：python -m pytest tests -v -o "addopts="
"""
import importlib
import json
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import private_message

from fake_openai import FakeChoice, FakeMessage, FakeOpenAIClient, FakeResponse, FakeToolCall, make_client

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "200200"
SESSION_KEY = f"OpenAIChatPlugin:daily"


def _harness() -> PluginTestHarness:
    return PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR)


def _tool_module(plugin):
    """拿到插件实际加载的 tools 模块（避免重复 import 出两份模块对象）"""
    return importlib.import_module(f"{plugin.__class__.__module__.rsplit('.', 1)[0]}.tools")


def _patch_scheduler(monkeypatch, plugin, *, add_ok=True, remove_ok=True):
    """替换框架调度器调用，记录注册/删除参数"""
    added = []
    removed = []

    def fake_add(name, interval, callback=None):
        added.append({"name": name, "interval": interval, "callback": callback})
        return add_ok

    def fake_remove(name):
        removed.append(name)
        return remove_ok

    monkeypatch.setattr(plugin, "add_scheduled_task", fake_add)
    monkeypatch.setattr(plugin, "remove_scheduled_task", fake_remove)
    return added, removed


async def test_tool_visibility_follows_config(tmp_path, monkeypatch):
    """AllowScheduledTasks 关闭时 manage_cron_tasks 不暴露给模型"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)

        names_off = {t["function"]["name"]
                     for t in plugin_tools.get_available_tools(allow_scheduled_tasks=False)}
        names_on = {t["function"]["name"]
                    for t in plugin_tools.get_available_tools(allow_scheduled_tasks=True)}

        assert "manage_cron_tasks" not in names_off
        assert "manage_cron_tasks" in names_on
        assert names_off <= names_on


async def test_add_list_delete_persisted(tmp_path, monkeypatch):
    """add 写入 data.json 并注册调度器；list 返回记录；delete 清理注册与记录"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        added, removed = _patch_scheduler(monkeypatch, plugin)

        payload = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="daily", interval="1h", prompt="提醒我喝水",
            session_type="group", session_id=GROUP_ID))
        assert payload["status"] == "success"

        assert added[0]["name"] == SESSION_KEY
        assert added[0]["interval"] == "1h"
        assert callable(added[0]["callback"])

        record = plugin.data["data"]["scheduled_tasks"]["daily"]
        assert record["prompt"] == "提醒我喝水"
        assert record["session_type"] == "group"
        assert record["session_id"] == GROUP_ID
        saved = json.loads((plugin.workspace / "data.json").read_text(encoding="utf-8"))
        assert saved["data"]["scheduled_tasks"]["daily"]["interval"] == "1h"

        listed = json.loads(await plugin_tools.manage_cron_tasks(plugin, action="list"))
        assert listed["status"] == "success"
        assert [item["name"] for item in listed["data"]] == ["daily"]

        removed_payload = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="delete", task_name="daily"))
        assert removed_payload["status"] == "success"
        assert removed == [SESSION_KEY]
        assert plugin.data["data"]["scheduled_tasks"] == {}


async def test_add_rejects_invalid_input(tmp_path, monkeypatch):
    """非法任务名 / 重名 / 缺少 interval 或 prompt 时返回 error 且不落库"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        _patch_scheduler(monkeypatch, plugin)

        cases = [
            {"task_name": "bad name!", "interval": "1h", "prompt": "x"},
            {"task_name": "ok", "interval": "", "prompt": "x"},
            {"task_name": "ok", "interval": "1h", "prompt": ""},
        ]
        for kwargs in cases:
            payload = json.loads(await plugin_tools.manage_cron_tasks(plugin, action="add", **kwargs))
            assert payload["status"] == "error"
        assert plugin.data["data"]["scheduled_tasks"] == {}

        ok = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="ok", interval="1h", prompt="x",
            session_type="group", session_id=GROUP_ID))
        assert ok["status"] == "success"
        dup = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="ok", interval="1h", prompt="x",
            session_type="group", session_id=GROUP_ID))
        assert dup["status"] == "error"


async def test_add_reports_scheduler_failure(tmp_path, monkeypatch):
    """框架注册失败（interval 非法/服务不可用）时返回 error 且不落库"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        _patch_scheduler(monkeypatch, plugin, add_ok=False)

        payload = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="daily", interval="bad", prompt="x"))
        assert payload["status"] == "error"
        assert plugin.data["data"]["scheduled_tasks"] == {}


@pytest.mark.parametrize("session_type, action", [
    ("group", "send_group_msg"),
    ("private", "send_private_msg"),
])
async def test_callback_delivers_generated_reply(tmp_path, monkeypatch, session_type, action):
    """定时任务到点：调用模型生成内容并投递到创建者会话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin._default_client = make_client("记得喝水")
        target = GROUP_ID if session_type == "group" else ROOT_QQ

        await plugin_tools._cron_task_callback(
            plugin, "daily", "提醒我喝水", session_type, target)

        h.assert_api(action).with_text("记得喝水")


async def test_callback_skips_empty_reply(tmp_path, monkeypatch):
    """模型返回空内容时不投递，避免空消息"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin._default_client = make_client("   ")

        await plugin_tools._cron_task_callback(plugin, "daily", "提醒", "group", GROUP_ID)

        h.assert_api("send_group_msg").not_called()


async def test_restore_scheduled_tasks_on_load(tmp_path, monkeypatch):
    """持久化记录在重启后重新注册到调度器（不受 AllowScheduledTasks 开关影响）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        added, _ = _patch_scheduler(monkeypatch, plugin)
        plugin.config["AllowScheduledTasks"] = False
        plugin.data["data"]["scheduled_tasks"]["daily"] = {
            "interval": "2h",
            "prompt": "提醒我喝水",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        plugin._restore_scheduled_tasks()

        assert added[0]["name"] == SESSION_KEY
        assert added[0]["interval"] == "2h"


async def test_dispatch_blocked_when_disabled(tmp_path, monkeypatch):
    """AllowScheduledTasks 未开启时，模型发起的 manage_cron_tasks 调用被拒绝"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        _patch_scheduler(monkeypatch, plugin)
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "manage_cron_tasks",
                    '{"action": "add", "task_name": "daily", "interval": "1h", "prompt": "x"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="好的"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("每小時提醒我", user_id=ROOT_QQ))
        await h.settle()

        session = plugin.data["data"]["user_conversations"][ROOT_QQ]
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "AllowScheduledTasks" in tool_entry["content"]
        assert plugin.data["data"]["scheduled_tasks"] == {}

        # 未开启时工具 schema 也不应出现在请求里
        tool_names = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
        assert "manage_cron_tasks" not in tool_names


async def test_dispatch_creates_task_when_enabled(tmp_path, monkeypatch):
    """AllowScheduledTasks 开启时，模型可通过工具创建任务（目标锁定当前会话）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowScheduledTasks"] = True
        added, _ = _patch_scheduler(monkeypatch, plugin)
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "manage_cron_tasks",
                    '{"action": "add", "task_name": "daily", "interval": "1h", "prompt": "提醒我喝水"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="已设置"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("每小时提醒我喝水", user_id=ROOT_QQ))
        await h.settle()

        assert added[0]["name"] == SESSION_KEY
        record = plugin.data["data"]["scheduled_tasks"]["daily"]
        assert record["session_type"] == "private"
        assert record["session_id"] == ROOT_QQ
