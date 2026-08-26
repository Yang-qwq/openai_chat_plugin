# -*- coding: utf-8 -*-
"""命令流程测试：/chat 用户命令、/chat-admin 权限拦截、/chatrbac 管理。

事件经框架 MockAdapter 注入走真实分发链路，回复文本用 assert_api().with_text() 断言。
运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message, private_message

pytestmark = pytest.mark.asyncio(mode="strict")

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
PERM = "OpenAIChatPlugin.admin"
ROOT_QQ = "123456"
GROUP_ID = "200200"


def _harness() -> PluginTestHarness:
    """构造隔离的插件测试环境"""
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


async def test_chat_help_private(tmp_path, monkeypatch):
    """私聊 /chat help 返回用户帮助文本"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(private_message("/chat help", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").called().with_text("用户命令帮助")


async def test_chat_unknown_subcommand(tmp_path, monkeypatch):
    """/chat 未知子命令返回未知命令提示"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(private_message("/chat foo", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("未知命令")


async def test_chat_set_present_not_exist(tmp_path, monkeypatch):
    """/chat set-present 指定不存在的预设时返回错误提示"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(private_message("/chat set-present nosuch", user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_private_msg").with_text("预设 nosuch 不存在")


async def test_chat_set_present_and_reset(tmp_path, monkeypatch):
    """/chat set-present 设置默认预设成功，/chat reset 重置会话成功"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # 设置预设（默认预设由 on_load 自动创建）
        await h.inject(group_message("/chat set-present default", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").with_text("已设置当前预设为: default")
        assert plugin.data["data"]["group_preset_names"][GROUP_ID] == "default"

        # 重置会话
        h.reset_api()
        await h.inject(group_message("/chat reset", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").with_text("已重置当前会话")


async def test_chat_admin_denied_for_normal_user_private(tmp_path, monkeypatch):
    """非管理员私聊使用 /chat-admin 被拒绝（fail-closed，不查询群角色）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(private_message("/chat-admin help", user_id="999999"))
        await h.settle()
        h.assert_api("send_private_msg").with_text("权限不足")
        # 私聊路径不应触发群成员列表查询
        h.assert_api("get_group_member_list").not_called()


async def test_chat_admin_root_allowed(tmp_path, monkeypatch):
    """root 使用 /chat-admin help 正常返回帮助文本"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        await h.inject(group_message("/chat-admin help", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").with_text("管理员命令帮助")


async def test_chatrbac_grant_revoke_list(tmp_path, monkeypatch):
    """root 可通过 /chatrbac 授予/撤销/查看全局管理员"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        # root 授予 888888
        await h.inject(group_message("/chatrbac grant 888888", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        assert plugin.check_permission("888888", PERM) is True
        h.assert_api("send_group_msg").with_text("已授予 888888")

        # list 能看到 root 与 888888
        h.reset_api()
        await h.inject(group_message("/chatrbac list", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").with_text("全局管理员列表").with_text(ROOT_QQ, "888888")

        # 撤销 888888
        h.reset_api()
        await h.inject(group_message("/chatrbac revoke 888888", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        assert plugin.check_permission("888888", PERM) is False
        h.assert_api("send_group_msg").with_text("已撤销 888888")

        # /chatrbac 全程为严格 RBAC 路径，不查询群成员列表
        h.assert_api("get_group_member_list").not_called()


async def test_chatrbac_invalid_target_rejected(tmp_path, monkeypatch):
    """/chatrbac 目标为 @全体成员 或非法文本时拒绝授权"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)

        await h.inject(group_message("/chatrbac grant all", group_id=GROUP_ID, user_id=ROOT_QQ))
        await h.settle()
        h.assert_api("send_group_msg").with_text("纯数字或 @ 成员")
        assert plugin.check_permission("all", PERM) is False


async def test_chatrbac_denied_for_non_admin(tmp_path, monkeypatch):
    """非全局管理员（即使群主）无法使用 /chatrbac，且严格路径不查询群角色"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        from ncatbot.types.napcat.group import GroupMemberInfo
        h.mock_api_for("qq").set_response(
            "get_group_member_list",
            [GroupMemberInfo(user_id="444", role="owner", group_id=GROUP_ID)],
        )

        await h.inject(group_message("/chatrbac grant 888888", group_id=GROUP_ID, user_id="444"))
        await h.settle()
        h.assert_api("send_group_msg").with_text("权限不足")
        # 防提权：/chatrbac 不查询群角色
        h.assert_api("get_group_member_list").not_called()
