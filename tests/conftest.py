# -*- coding: utf-8 -*-
"""pytest 全局配置：测试环境隔离

在导入 ncatbot 之前指定配置文件路径，避免读取/污染仓库根目录的真实 config.yaml。
"""
import os
import tempfile
from pathlib import Path

os.environ["NCATBOT_CONFIG_PATH"] = str(
    Path(__file__).resolve().parent / "ncatbot_test_config.yaml"
)

PLUGINS_DIR = Path(__file__).resolve().parents[2]

import pytest


@pytest.fixture(scope="session", autouse=True)
def _warm_first_plugin_load():
    """预热：正式用例前先空载完成一次插件加载/卸载。

    背景：pytest 收集阶段会以"无 loader 上下文"的方式提前导入本插件包
    （插件目录本身是 Python 包，__init__.py 触发 main.py 导入并执行
    @registrar 装饰器，handler 被收集到 '__global__' 键下）。这导致首个
    PluginTestHarness 会话中 flush_pending('OpenAIChatPlugin') 取不到任何
    pending handler，分发器为空、消息事件无人响应。

    本夹具在临时目录中先行启动/关闭一次 harness：框架卸载时会清除
    sys.modules 中的插件模块，后续每个真实会话都会在正确的 loader 上下文中
    重新导入并注册 handler。

    全程使用临时工作目录，不触碰仓库真实 data/。
    """
    import asyncio

    from ncatbot.testing import PluginTestHarness

    prev_cwd = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="openai_chat_warmup_")
    os.chdir(tmp)
    try:

        async def _run():
            async with PluginTestHarness(
                plugin_names=["OpenAIChatPlugin"],
                plugins_dir=PLUGINS_DIR,
            ) as h:
                # 仅验证可加载；handler 注册情况由各用例自行断言
                assert h.get_plugin("OpenAIChatPlugin") is not None

        asyncio.run(_run())
    finally:
        os.chdir(prev_cwd)
