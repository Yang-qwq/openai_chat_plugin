# -*- coding: utf-8 -*-
"""插件加载冒烟测试：manifest 可解析、插件可加载、root 自动授权、命令已注册。

运行：python -m pytest tests -v -o "addopts="
"""
import tomllib
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness

pytestmark = pytest.mark.asyncio(mode="strict")

# 插件根目录的上一级（仓库 plugins/ 目录，内含本插件 manifest.toml）
PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGIN_NAME = "OpenAIChatPlugin"
PERM = "OpenAIChatPlugin.admin"
ROOT_QQ = "123456"


def _harness() -> PluginTestHarness:
    """构造隔离的插件测试环境（不启动，由调用方 async with 启动）"""
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


def test_manifest_matches_class_metadata():
    """manifest.toml 可解析且与入口类元数据一致"""
    manifest = tomllib.loads((PLUGIN_DIR / "manifest.toml").read_text(encoding="utf-8"))
    assert manifest["name"] == PLUGIN_NAME
    assert manifest["main"] == "main.py"
    assert manifest["entry_class"] == "OpenAIChatPlugin"
    # main.py 导入成功后校验类属性与 manifest 一致
    import sys

    sys.path.insert(0, str(PLUGINS_DIR))
    try:
        module = __import__(f"{PLUGIN_NAME}.main", fromlist=["OpenAIChatPlugin"])
        cls = module.OpenAIChatPlugin
        assert cls.name == manifest["name"]
        assert cls.version == manifest["version"]
    finally:
        sys.path.pop(0)


async def test_plugin_loads_and_grants_root(tmp_path, monkeypatch):
    """插件经框架加载成功，root 自动获得全局管理员权限，非 root 无权限"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        assert plugin is not None
        assert plugin.check_permission(ROOT_QQ, PERM) is True
        assert plugin.check_permission("999999", PERM) is False


async def test_default_preset_created(tmp_path, monkeypatch):
    """首次加载自动创建默认预设（config.yaml + 空 prompt.md）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        present_dir = plugin.workspace / "presents" / "default"
        assert (present_dir / "config.yaml").is_file()
        assert (present_dir / "prompt.md").is_file()
