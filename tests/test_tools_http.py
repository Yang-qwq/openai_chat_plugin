# -*- coding: utf-8 -*-
"""create_http_request 单元测试：用 httpx.MockTransport 隔离真实网络。

运行：python -m pytest tests -v -o "addopts="
"""
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as plugin_tools  # noqa: E402

pytestmark = pytest.mark.asyncio

# 1x1 透明 PNG（仅用于类型测试，不校验图片有效性）
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


def _payload(result: str) -> dict:
    return json.loads(result)


async def test_text_request_sends_body_headers_and_params():
    """text：返回文本内容，data 以 JSON 请求体发送，headers/params 生效"""
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen['method'] = request.method
        seen['url'] = str(request.url)
        seen['header'] = request.headers.get('x-test')
        seen['body'] = json.loads(request.content)
        return httpx.Response(
            200, text='hello', headers={'content-type': 'text/plain; charset=utf-8'})

    result = await plugin_tools.create_http_request(
        'get', 'https://example.com/path',
        content_format='text',
        data={'a': 1},
        headers={'X-Test': 'yes'},
        params={'q': 'hi'},
        transport=_transport(handler),
    )

    payload = _payload(result)
    assert payload['status'] == 'success'
    assert payload['message'] == '200 OK'
    assert payload['data']['content'] == 'hello'
    assert seen['method'] == 'GET'
    assert seen['url'] == 'https://example.com/path?q=hi'
    assert seen['header'] == 'yes'
    # httpx 在 GET 上也会发送显式提供的 json body
    assert seen['body'] == {'a': 1}


async def test_json_response_success_and_invalid_json():
    """json：合法响应解析为对象；非法 JSON 返回 error payload"""

    async def ok_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={'ok': True})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/api', content_format='json',
        transport=_transport(ok_handler)))
    assert payload['status'] == 'success'
    assert payload['data'] == {'ok': True}

    async def bad_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='not-json', headers={'content-type': 'application/json'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/api', content_format='json',
        transport=_transport(bad_handler)))
    assert payload['status'] == 'error'
    assert 'JSON' in payload['message']
    assert payload['data']['content'] == 'not-json'


async def test_markdown_converts_html():
    """markdown：HTML 响应自动转换为 Markdown"""
    html = '<html><body><h1>标题</h1><p>正文</p></body></html>'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, headers={'content-type': 'text/html; charset=utf-8'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/page', content_format='markdown',
        transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert '# 标题' in payload['data']['content']


async def test_markdown_keeps_plain_markdown():
    """markdown：已经是 Markdown 的响应保持不变"""
    text = '# 标题\n\n- 一\n- 二'

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=text, headers={'content-type': 'text/markdown'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/page', content_format='markdown',
        transport=_transport(handler)))
    assert payload['data']['content'] == text


async def test_image_returns_vision_payload():
    """image：图片响应返回 data URL，并可被 extract_vision_payload 识别"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={'content-type': 'image/png'})

    result = await plugin_tools.create_http_request(
        'GET', 'https://example.com/a.png', content_format='image',
        transport=_transport(handler))
    payload = _payload(result)
    assert payload['status'] == 'success'
    assert payload['data']['type'] == 'image'
    assert payload['data']['mime_type'] == 'image/png'
    assert payload['data']['size'] == len(PNG_BYTES)
    assert payload['data']['data_url'].startswith('data:image/png;base64,')

    vision = plugin_tools.extract_vision_payload(result)
    assert vision is not None
    assert vision['data_url'] == payload['data']['data_url']


async def test_image_rejects_non_image_response():
    """image：响应 Content-Type 不是支持的图片时返回 error"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='<html></html>', headers={'content-type': 'text/html'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/a', content_format='image',
        transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert '图片类型' in payload['message']


async def test_oversized_response_is_rejected(monkeypatch):
    """超出大小限制的响应被中止，避免撑爆上下文"""
    monkeypatch.setattr(plugin_tools, 'MAX_TEXT_RESPONSE_BYTES', 16)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=b'x' * 32)

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/big', content_format='text',
        transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert '过大' in payload['message']


async def test_http_error_status_returns_snippet():
    """非 2xx：返回 error payload 并附带响应片段"""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(404, text='not found', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/missing', content_format='text',
        transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert payload['message'].startswith('404')
    assert payload['data']['content'] == 'not found'


async def test_timeout_and_network_error_are_caught():
    """网络异常（超时/连接失败）转为 error payload，不向上抛异常"""

    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('slow', request=request)

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/slow', content_format='text',
        transport=_transport(timeout_handler)))
    assert payload['status'] == 'error'
    assert '超时' in payload['message']

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom', request=request)

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/err', content_format='text',
        transport=_transport(error_handler)))
    assert payload['status'] == 'error'
    assert 'HTTP 请求失败' in payload['message']


@pytest.mark.parametrize('method,url,content_format', [
    ('TRACE', 'https://example.com', 'text'),
    ('GET', 'ftp://example.com/file', 'text'),
    ('GET', 'not-a-url', 'text'),
    ('GET', 'https://user:pass@example.com', 'text'),
    ('GET', 'https://example.com', 'bytes'),
])
async def test_invalid_arguments_are_rejected(method, url, content_format):
    """非法方法/URL/content_format 在发请求前被拒绝"""
    payload = _payload(await plugin_tools.create_http_request(
        method, url, content_format=content_format))
    assert payload['status'] == 'error'


async def test_extract_vision_payload_ignores_plain_result():
    """非图片工具结果不会被误判为视觉消息"""
    assert plugin_tools.extract_vision_payload(
        plugin_tools._generate_tool_payload('success', 'ok', {'content': 'hi'})) is None
    assert plugin_tools.extract_vision_payload('not-json') is None
