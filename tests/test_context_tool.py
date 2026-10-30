# -*- coding: utf-8 -*-
"""query_missing_message_context 取回条数测试

覆盖：默认条数、模型指定 count、上限 clamp 到 MaxConversationCacheStoreAmount、
非法参数回退、空缓存，以及插件分派链确实注入了配置上限。

运行：python -m pytest tests -v -o "addopts="
"""
import json
import sys
from pathlib import Path

import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
    FakeToolCall,
)

PLUGIN_DIR = Path(__file__).resolve().parents[1]
PLUGINS_DIR = PLUGIN_DIR.parent
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as plugin_tools  # noqa: E402

pytestmark = pytest.mark.asyncio

PLUGIN_NAME = "OpenAIChatPlugin"
GROUP_ID = "200200"
SELF_ID = "10001"


def _payload(result: str) -> dict:
    return json.loads(result)


async def test_default_returns_latest_default_count():
    history = [f'消息{i}' for i in range(plugin_tools.DEFAULT_MISSING_CONTEXT_COUNT + 25)]
    expected = history[-plugin_tools.DEFAULT_MISSING_CONTEXT_COUNT:]
    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: history}, GROUP_ID))

    assert payload['status'] == 'success'
    assert len(payload['data']) == plugin_tools.DEFAULT_MISSING_CONTEXT_COUNT
    assert payload['data'] == expected
    assert '最近' in payload['message']


async def test_count_returns_latest_n():
    history = [f'消息{i}' for i in range(10)]
    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: history}, GROUP_ID, count=3))

    assert payload['data'] == ['消息7', '消息8', '消息9']
    assert '最近3条' in payload['message']


async def test_count_clamped_to_max_count():
    history = [f'消息{i}' for i in range(100)]
    expected = history[-30:]
    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: history}, GROUP_ID, count=80, max_count=30))

    assert len(payload['data']) == 30
    assert payload['data'] == expected


async def test_invalid_count_falls_back_and_non_positive_clamps_to_one():
    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: [f'消息{i}' for i in range(5)]}, GROUP_ID, count='abc'))
    assert payload['data'] == [f'消息{i}' for i in range(5)]

    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: [f'消息{i}' for i in range(5)]}, GROUP_ID, count=0))
    assert payload['data'] == ['消息4']


async def test_empty_history_returns_empty_data():
    payload = _payload(await plugin_tools.query_missing_message_context(
        {GROUP_ID: []}, GROUP_ID))

    assert payload['status'] == 'success'
    assert payload['data'] == []


async def test_successful_fetch_clears_entire_group_cache():
    """取回成功后清空该群全部临时消息，而不是只删返回的那几条"""
    kv = {GROUP_ID: [f'消息{i}' for i in range(10)]}

    payload = _payload(await plugin_tools.query_missing_message_context(
        kv, GROUP_ID, count=3))
    assert payload['data'] == ['消息7', '消息8', '消息9']
    assert kv[GROUP_ID] == []

    # 缓存保留空列表：再次查询仍是 success + 空数组，不会因为 key 消失报错
    again = _payload(await plugin_tools.query_missing_message_context(kv, GROUP_ID))
    assert again['status'] == 'success'
    assert again['data'] == []


async def test_truncated_fetch_also_clears_all_messages():
    kv = {GROUP_ID: [f'消息{i}' for i in range(100)]}

    payload = _payload(await plugin_tools.query_missing_message_context(
        kv, GROUP_ID, count=2, max_count=10))
    assert payload['data'] == ['消息98', '消息99']
    assert kv[GROUP_ID] == []


async def test_dispatch_caps_count_by_configured_cache_size(tmp_path, monkeypatch):
    """插件分派链把 MaxConversationCacheStoreAmount 作为取回条数上限"""
    monkeypatch.chdir(tmp_path)
    async with PluginTestHarness(plugin_names=[PLUGIN_NAME], plugins_dir=PLUGINS_DIR) as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        plugin.config['EnableBuiltinFunctionCalling'] = True
        plugin.config['MaxConversationCacheStoreAmount'] = 5
        # 临时缓存本身也会被该配置截断；这里预置 20 条模拟历史
        plugin.temp_history_messages_kv[GROUP_ID] = [f'消息{i}' for i in range(20)]

        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    'call_ctx', 'query_missing_message_context', '{"count": 100}')],
            ), 'tool_calls')),
            FakeResponse(FakeChoice(FakeMessage(content='好的'), 'stop')),
        ])
        plugin._default_client = client

        await h.inject(group_message(
            '在吗',
            group_id=GROUP_ID,
            user_id='99999',
            self_id=SELF_ID,
            raw_message=f'[CQ:at,qq={SELF_ID}] 在吗',
            message=[
                {'type': 'at', 'data': {'qq': SELF_ID}},
                {'type': 'text', 'data': {'text': ' 在吗'}},
            ],
        ))
        await h.settle()

        tool_messages = [
            message for message in client.chat.completions.calls[1]['messages']
            if message['role'] == 'tool'
        ]
        payload = _payload(tool_messages[0]['content'])
        assert payload['status'] == 'success'
        assert len(payload['data']) == 5
        assert payload['data'][-1].endswith('在吗')
        # 取回成功即消费：该群临时缓存被清空
        assert plugin.temp_history_messages_kv[GROUP_ID] == []
