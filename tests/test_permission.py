# -*- coding: utf-8 -*-
"""权限体系测试：三层授权（RBAC 全局管理员 > 群主/群管理自动放行 > 默认拒绝）。

覆盖：
1. root（config.yaml 中机器人 owner）启动时自动获得全局管理员权限
2. 群主/群管理自动放行 /chat-admin（受 EnableGroupOwnerAutoAuth 开关控制，带 TTL 缓存）
3. /chatrbac 严格 RBAC 路径不查询群角色（防提权）

运行：python -m pytest tests -v -o "addopts="
"""
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness, group_message
from ncatbot.types.napcat.group import GroupMemberInfo

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


def _member_list(roles):
    """构造群成员列表（user_id -> role）"""
    return [
        GroupMemberInfo(user_id=uid, role=role, group_id=GROUP_ID)
        for uid, role in roles.items()
    ]


async def test_root_granted_on_load(tmp_path, monkeypatch):
    """root 在插件加载后自动获得全局管理员权限，非 root 无权限"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        assert plugin.check_permission(ROOT_QQ, PERM) is True
        assert plugin.check_permission("999999", PERM) is False


async def test_group_owner_and_admin_can_use_admin_command(tmp_path, monkeypatch):
    """群主/群管理经 MockAdapter 查询群成员角色后自动放行，普通成员被拒"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        mock = h.mock_api_for("qq")
        mock.set_response(
            "get_group_member_list",
            _member_list({"111": "owner", "222": "admin", "333": "member"}),
        )

        # 群主放行：经真实 API 查询群角色
        await h.inject(group_message("/chat-admin help", group_id=GROUP_ID, user_id="111"))
        await h.settle()
        h.assert_api("get_group_member_list").called().with_params(group_id=GROUP_ID)
        h.assert_api("send_group_msg").with_text("管理员命令帮助")

        # 群管理放行：命中 TTL 角色缓存，不再重复请求群成员列表
        await h.inject(group_message("/chat-admin help", group_id=GROUP_ID, user_id="222"))
        await h.settle()
        assert mock.call_count("get_group_member_list") == 1
        h.assert_api("send_group_msg").with_text("管理员命令帮助")

        # 普通成员拒绝：角色为 member，回复权限不足
        await h.inject(group_message("/chat-admin help", group_id=GROUP_ID, user_id="333"))
        await h.settle()
        assert mock.call_count("get_group_member_list") == 1
        h.assert_api("send_group_msg").with_text("权限不足")


async def test_group_owner_auto_auth_can_be_disabled(tmp_path, monkeypatch):
    """EnableGroupOwnerAutoAuth 关闭后群主不再自动放行（且不查询群成员列表）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        h.mock_api_for("qq").set_response(
            "get_group_member_list", _member_list({"111": "owner"})
        )
        plugin.config["EnableGroupOwnerAutoAuth"] = False

        await h.inject(group_message("/chat-admin help", group_id=GROUP_ID, user_id="111"))
        await h.settle()
        h.assert_api("send_group_msg").with_text("权限不足")
        h.assert_api("get_group_member_list").not_called()


async def test_chatrbac_strict_rbac_no_group_role_query(tmp_path, monkeypatch):
    """/chatrbac 仅走严格 RBAC：群主身份不能越权授予全局管理员"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        h.mock_api_for("qq").set_response(
            "get_group_member_list", _member_list({"444": "owner"})
        )

        await h.inject(group_message("/chatrbac grant 888888", group_id=GROUP_ID, user_id="444"))
        await h.settle()
        assert plugin.check_permission("888888", PERM) is False
        h.assert_api("send_group_msg").with_text("权限不足")
        # 防提权：即使对方是群主，/chatrbac 也不查询群角色
        h.assert_api("get_group_member_list").not_called()
