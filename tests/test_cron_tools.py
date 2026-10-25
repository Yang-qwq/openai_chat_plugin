# -*- coding: utf-8 -*-
"""AI 定时任务工具测试

覆盖：工具可见性开关、增删查与持久化、注册失败处理、到点生成并投递、
重启恢复（重新注册）以及未开启时的分派拦截。

运行：python -m pytest tests -v -o "addopts="
"""
import asyncio
import importlib
import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message, private_message

from fake_openai import FakeChoice, FakeMessage, FakeOpenAIClient, FakeResponse, FakeToolCall, make_client

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "200200"
SESSION_KEY = "OpenAIChatPlugin:daily"


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


def _failing_client(message="boom"):
    """构造 chat.completions.create 必然抛异常的客户端 stub"""

    class _Client:
        def __init__(self):
            self.chat = SimpleNamespace(
                completions=SimpleNamespace(create=self._create))

        @staticmethod
        def _create(**_kwargs):
            raise RuntimeError(message)

    return _Client()


async def test_all_tools_exposed(tmp_path, monkeypatch):
    """工具 schema 始终完整暴露，开关只影响实际调用（由分派链返回不可调用提示）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)

        names = {tool["function"]["name"] for tool in plugin_tools.tools}
        assert {"manage_cron_tasks", "create_http_request", "access_memory"} <= names


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
            plugin, action="delete", task_name="daily",
            session_type="group", session_id=GROUP_ID))
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


async def test_callback_uses_main_history_and_writes_back(tmp_path, monkeypatch):
    """定时轮使用主会话历史，成功后写入内部消息与回复，且请求不带 tools"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin._default_client = make_client("该喝水了")
        history = [
            {"role": "system", "content": "预设提示"},
            {"role": "user", "content": "早上好"},
            {"role": "assistant", "content": "早上好呀"},
        ]
        plugin.data["data"]["group_conversations"][GROUP_ID] = history

        await plugin_tools._cron_task_callback(
            plugin, "daily", "提醒我喝水", "group", GROUP_ID)

        assert history[-2] == {"role": "user", "content": "[定时任务 daily] 提醒我喝水"}
        assert history[-1] == {"role": "assistant", "content": "该喝水了"}

        call = plugin._default_client.chat.completions.calls[0]
        assert [m["role"] for m in call["messages"]] == ["system", "user", "assistant", "user"]
        assert call["messages"][-1]["content"] == "[定时任务 daily] 提醒我喝水"
        # 定时轮不传工具参数，兼容严格网关（`"tools": null` 会被拒绝）
        assert "tools" not in call
        assert "tool_choice" not in call
        h.assert_api("send_group_msg").with_text("该喝水了")


async def test_callback_failure_does_not_pollute_history(tmp_path, monkeypatch):
    """生成/投递失败时不写入历史，只累计 failures（成功才落库）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin._default_client = _failing_client()
        history = [
            {"role": "system", "content": "预设提示"},
            {"role": "user", "content": "早上好"},
        ]
        plugin.data["data"]["group_conversations"][GROUP_ID] = history
        plugin.data["data"]["scheduled_tasks"]["daily"] = {
            "interval": "1h",
            "prompt": "提醒",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        await plugin_tools._cron_task_callback(
            plugin, "daily", "提醒", "group", GROUP_ID)

        assert plugin.data["data"]["group_conversations"][GROUP_ID] == history
        assert plugin.data["data"]["scheduled_tasks"]["daily"]["failures"] == 1
        h.assert_api("send_group_msg").not_called()


async def test_scheduled_turn_supports_tool_calls(tmp_path, monkeypatch):
    """定时轮也走工具循环：工具结果进入下一轮请求，成功后整轮写入历史"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content="我先查一下时间",
                tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage(content="时间已查好"), "stop")),
        ])
        plugin._default_client = client

        await plugin_tools._cron_task_callback(
            plugin, "daily", "提醒我喝水", "group", GROUP_ID)

        # 两轮请求都携带工具；第二轮能看到内部消息与工具结果
        assert all("tools" in call for call in client.chat.completions.calls)
        second_messages = client.chat.completions.calls[1]["messages"]
        assert second_messages[0] == {
            "role": "user", "content": "[定时任务 daily] 提醒我喝水"}
        assert any(m["role"] == "tool" and m["name"] == "get_system_time"
                   for m in second_messages)

        # 中间正文与最终回复都投递
        h.assert_api("send_group_msg").with_text("我先查一下时间")
        h.assert_api("send_group_msg").with_text("时间已查好")

        # 整轮写入历史：内部消息 → assistant(tool_calls) → tool → assistant 最终
        history = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert history[0] == {"role": "user", "content": "[定时任务 daily] 提醒我喝水"}
        assert history[1]["role"] == "assistant" and history[1].get("tool_calls")
        assert history[2]["role"] == "tool" and history[2]["name"] == "get_system_time"
        assert history[3] == {"role": "assistant", "content": "时间已查好"}


async def test_scheduled_turn_tool_failure_keeps_history(tmp_path, monkeypatch):
    """工具轮中途失败时整轮不落库，只累计 failures"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin.config["EnableBuiltinFunctionCalling"] = True
        # 只提供一轮响应：第二次请求时 FakeCompletions.pop 越界抛异常
        plugin._default_client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall("call_1", "get_system_time", "{}")],
            ), "tool_calls")),
        ])
        history = [{"role": "system", "content": "预设提示"}]
        plugin.data["data"]["group_conversations"][GROUP_ID] = history
        plugin.data["data"]["scheduled_tasks"]["daily"] = {
            "interval": "1h",
            "prompt": "提醒",
            "session_type": "group",
            "session_id": GROUP_ID,
            "creator_id": ROOT_QQ,
            "created_at": "2026-01-01T00:00:00Z",
        }

        await plugin_tools._cron_task_callback(
            plugin, "daily", "提醒", "group", GROUP_ID)

        assert plugin.data["data"]["group_conversations"][GROUP_ID] == history
        assert plugin.data["data"]["scheduled_tasks"]["daily"]["failures"] == 1
        h.assert_api("send_group_msg").not_called()


async def test_scheduled_turn_serialized_per_session(tmp_path, monkeypatch):
    """同一会话的定时轮次串行执行：后一轮请求能看到前一轮写入的历史"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)

        class SlowClient:
            """每次调用阻塞 0.05s，并在返回时带出调用序号"""

            def __init__(self):
                self.calls = []
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create))

            def create(self, **kwargs):
                self.calls.append(kwargs)
                index = len(self.calls)
                time.sleep(0.05)
                return FakeResponse(FakeChoice(FakeMessage(content=f"第{index}条"), "stop"))

        client = SlowClient()
        plugin._default_client = client

        await asyncio.gather(
            plugin_tools._cron_task_callback(plugin, "first", "提醒1", "group", GROUP_ID),
            plugin_tools._cron_task_callback(plugin, "second", "提醒2", "group", GROUP_ID),
        )

        contents = [m.get("content") for m in client.calls[1]["messages"]]
        assert "[定时任务 first] 提醒1" in contents
        assert "第1条" in contents

        history = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert [(m["role"], m["content"]) for m in history[-4:]] == [
            ("user", "[定时任务 first] 提醒1"),
            ("assistant", "第1条"),
            ("user", "[定时任务 second] 提醒2"),
            ("assistant", "第2条"),
        ]


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
    """AllowScheduledTasks 未开启时，工具仍然可见，但调用会收到不可用提示"""
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
        assert "无法管理定时任务" in tool_entry["content"]
        assert plugin.data["data"]["scheduled_tasks"] == {}

        # 工具 schema 始终暴露给模型，由分派链负责拒绝并给出提示
        tool_names = {t["function"]["name"] for t in client.chat.completions.calls[0]["tools"]}
        assert "manage_cron_tasks" in tool_names


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
        assert record["creator_id"] == ROOT_QQ


async def test_add_enforces_minimum_interval(tmp_path, monkeypatch):
    """interval 小于 60 秒时拒绝创建（含 0.5s 被框架截断成 0 的情况）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        added, _ = _patch_scheduler(monkeypatch, plugin)

        for index, bad_interval in enumerate(["30s", "0.5s", "0", "1"]):
            payload = json.loads(await plugin_tools.manage_cron_tasks(
                plugin, action="add", task_name=f"bad{index}", interval=bad_interval,
                prompt="x", session_type="group", session_id=GROUP_ID))
            assert payload["status"] == "error", bad_interval
            assert "60" in payload["message"], bad_interval

        assert added == []
        assert plugin.data["data"]["scheduled_tasks"] == {}


async def test_add_accepts_valid_intervals(tmp_path, monkeypatch):
    """60 秒及以上、每日任务与足够远的一次性任务都可以创建"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        added, _ = _patch_scheduler(monkeypatch, plugin)

        later = (datetime.now() + timedelta(seconds=120)).strftime("%Y-%m-%d %H:%M:%S")
        cases = {
            "one_min": "1m",
            "hours": "2h30m",
            "half_day": "0.5d",
            "daily": "23:59",
            "once": later,
        }
        for name, interval in cases.items():
            payload = json.loads(await plugin_tools.manage_cron_tasks(
                plugin, action="add", task_name=name, interval=interval, prompt="x",
                session_type="group", session_id=GROUP_ID))
            assert payload["status"] == "success", (interval, payload)

        assert len(added) == len(cases)
        # 框架解析器不支持复合单位，插件累加换算成秒后注册
        assert any(item["interval"] == "9000s" for item in added)


async def test_add_rejects_too_soon_one_shot(tmp_path, monkeypatch):
    """一次性任务距离当前不足 60 秒同样拒绝创建"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        _patch_scheduler(monkeypatch, plugin)

        soon = (datetime.now() + timedelta(seconds=30)).strftime("%Y-%m-%d %H:%M:%S")
        payload = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="soon", interval=soon,
            prompt="x", session_type="group", session_id=GROUP_ID))
        assert payload["status"] == "error"
        assert plugin.data["data"]["scheduled_tasks"] == {}


async def test_list_visible_but_delete_limited_to_owner(tmp_path, monkeypatch):
    """任务列表全局可见（不做隔离），但只有创建它的会话能删除"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        _, removed = _patch_scheduler(monkeypatch, plugin)

        created = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="daily", interval="1h", prompt="x",
            session_type="group", session_id=GROUP_ID))
        assert created["status"] == "success"

        listed = json.loads(await plugin_tools.manage_cron_tasks(plugin, action="list"))
        assert [item["name"] for item in listed["data"]] == ["daily"]
        assert listed["data"][0]["session_id"] == GROUP_ID

        for session_type, session_id in (("group", "999999"), ("private", ROOT_QQ)):
            denied = json.loads(await plugin_tools.manage_cron_tasks(
                plugin, action="delete", task_name="daily",
                session_type=session_type, session_id=session_id))
            assert denied["status"] == "error"
            assert "不属于当前会话" in denied["message"]

        assert removed == []
        assert "daily" in plugin.data["data"]["scheduled_tasks"]

        deleted = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="delete", task_name="daily",
            session_type="group", session_id=GROUP_ID))
        assert deleted["status"] == "success"
        assert removed == [SESSION_KEY]
        assert plugin.data["data"]["scheduled_tasks"] == {}


async def test_restore_skips_interval_below_minimum(tmp_path, monkeypatch):
    """历史记录中 < 60 秒的任务不会被注册，记录保留以便排查或删除"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        added, _ = _patch_scheduler(monkeypatch, plugin)
        plugin.data["data"]["scheduled_tasks"]["legacy"] = {
            "interval": "30s",
            "prompt": "太频繁了",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        plugin._restore_scheduled_tasks()

        assert added == []
        assert "legacy" in plugin.data["data"]["scheduled_tasks"]


async def test_callback_runs_blocking_client_in_thread(tmp_path, monkeypatch):
    """同步 OpenAI 调用不得阻塞事件循环（回调走 asyncio.to_thread）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)

        class SlowClient:
            """create() 同步阻塞 0.2s，用于检测是否卡住事件循环"""

            def __init__(self):
                self.chat = SimpleNamespace(
                    completions=SimpleNamespace(create=self.create))

            @staticmethod
            def create(**_kwargs):
                time.sleep(0.2)
                return FakeResponse(FakeChoice(FakeMessage(content="慢回复"), "stop"))

        plugin._default_client = SlowClient()
        gaps = []

        async def heartbeat():
            last = time.monotonic()
            for _ in range(10):
                await asyncio.sleep(0.02)
                now = time.monotonic()
                gaps.append(now - last)
                last = now

        await asyncio.gather(
            plugin_tools._cron_task_callback(
                plugin, "slow", "提醒", "group", GROUP_ID),
            heartbeat(),
        )

        assert max(gaps) < 0.15
        h.assert_api("send_group_msg").with_text("慢回复")


async def test_callback_disables_task_after_repeated_failures(tmp_path, monkeypatch):
    """连续失败达到上限后自动停用并移出调度器，恢复时跳过已停用记录"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        added, removed = _patch_scheduler(monkeypatch, plugin)

        plugin._default_client = _failing_client("boom")
        plugin.data["data"]["scheduled_tasks"]["daily"] = {
            "interval": "1h",
            "prompt": "提醒",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        for _ in range(plugin_tools.MAX_TASK_CONSECUTIVE_FAILURES):
            await plugin_tools._cron_task_callback(
                plugin, "daily", "提醒", "group", GROUP_ID)

        record = plugin.data["data"]["scheduled_tasks"]["daily"]
        assert record["failures"] == plugin_tools.MAX_TASK_CONSECUTIVE_FAILURES
        assert record["disabled"] is True
        assert removed[-1] == SESSION_KEY

        # 已停用记录不会被恢复注册
        plugin._restore_scheduled_tasks()
        assert added == []
        h.assert_api("send_group_msg").not_called()


async def test_callback_cleans_up_one_shot_record(tmp_path, monkeypatch):
    """一次性任务执行后清理记录，避免列表里残留僵尸任务（时间已过也要能识别）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        plugin._default_client = make_client("一次性提醒")
        fired_at = (datetime.now() - timedelta(seconds=5)).strftime("%Y-%m-%d %H:%M:%S")
        plugin.data["data"]["scheduled_tasks"]["once"] = {
            "interval": fired_at,
            "prompt": "提醒",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        await plugin_tools._cron_task_callback(
            plugin, "once", "提醒", "group", GROUP_ID)

        assert "once" not in plugin.data["data"]["scheduled_tasks"]
        history = plugin.data["data"]["group_conversations"][GROUP_ID]
        assert history[-2]["content"] == "[定时任务 once] 提醒"
        assert history[-1] == {"role": "assistant", "content": "一次性提醒"}
        h.assert_api("send_group_msg").with_text("一次性提醒")


async def test_add_blocked_when_execution_paused(tmp_path, monkeypatch):
    """执行总开关关闭时不允许创建新任务"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin_tools = _tool_module(plugin)
        added, _ = _patch_scheduler(monkeypatch, plugin)
        plugin.config["EnableScheduledTaskExecution"] = False

        payload = json.loads(await plugin_tools.manage_cron_tasks(
            plugin, action="add", task_name="daily", interval="1h", prompt="x",
            session_type="group", session_id=GROUP_ID))

        assert payload["status"] == "error"
        assert "暂停" in payload["message"]
        assert added == []


async def test_restore_skipped_when_execution_paused(tmp_path, monkeypatch):
    """执行总开关关闭时启动不注册任何任务"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        added, _ = _patch_scheduler(monkeypatch, plugin)
        plugin.config["EnableScheduledTaskExecution"] = False
        plugin.data["data"]["scheduled_tasks"]["daily"] = {
            "interval": "1h",
            "prompt": "提醒",
            "session_type": "group",
            "session_id": GROUP_ID,
            "created_at": "2026-01-01T00:00:00Z",
        }

        assert plugin._restore_scheduled_tasks() == 0
        assert added == []


async def test_admin_task_list_delete_pause_resume(tmp_path, monkeypatch):
    """管理员可通过 /chat-admin task 查看、跨会话删除、暂停与恢复任务"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        added, removed = _patch_scheduler(monkeypatch, plugin)
        # 避免测试写入全局 config.yaml
        monkeypatch.setattr(
            plugin, "set_config",
            lambda key, value: plugin.config.__setitem__(key, value))
        plugin.data["data"]["scheduled_tasks"].update({
            "daily": {
                "interval": "1h", "prompt": "提醒喝水", "session_type": "group",
                "session_id": GROUP_ID, "created_at": "2026-01-01T00:00:00Z",
            },
            "legacy": {
                "interval": "30s", "prompt": "太频繁", "session_type": "private",
                "session_id": ROOT_QQ, "created_at": "2026-01-01T00:00:00Z",
                "disabled": True, "failures": 5, "disabled_reason": "boom",
            },
        })

        await h.inject(private_message("/chat-admin task list", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("定时任务列表：").with_text(
            "daily", "legacy", "已停用")

        h.reset_api()
        await h.inject(private_message("/chat-admin task delete daily", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("已删除定时任务 daily")
        assert "daily" not in plugin.data["data"]["scheduled_tasks"]
        assert removed[-1] == SESSION_KEY

        h.reset_api()
        await h.inject(private_message("/chat-admin task pause", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("已暂停定时任务执行")
        assert plugin.get_config("EnableScheduledTaskExecution") is False
        assert removed[-1] == "OpenAIChatPlugin:legacy"

        h.reset_api()
        await h.inject(private_message("/chat-admin task resume", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("已恢复定时任务执行")
        assert plugin.get_config("EnableScheduledTaskExecution") is True
        # legacy 已停用，不会被重新注册
        assert added == []


async def test_admin_task_requires_global_admin(tmp_path, monkeypatch):
    """群主/群管理不能通过 /chat-admin task 跨群操作定时任务（防提权）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        from ncatbot.types.napcat.group import GroupMemberInfo

        plugin = h.get_plugin(PLUGIN_NAME)
        h.mock_api_for("qq").set_response(
            "get_group_member_list",
            [GroupMemberInfo(user_id="444", role="owner", group_id=GROUP_ID)],
        )

        await h.inject(group_message(
            "/chat-admin task pause", group_id=GROUP_ID, user_id="444"))
        await h.settle()

        h.assert_api("send_group_msg").with_text("权限不足")
        assert plugin.get_config("EnableScheduledTaskExecution") is True
