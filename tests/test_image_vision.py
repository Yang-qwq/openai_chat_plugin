# -*- coding: utf-8 -*-
"""用户图片识别测试：识别用户消息中的图片并作为视觉输入交给模型。

- fetch_image_data_url / _sniff_image_mime 单元测试（httpx.MockTransport 隔离真实网络）
- 对话链路：私聊/群聊 @机器人 携带图片时，模型收到 OpenAI 多模态 content
  （text + image_url data URL）；图片识别可经 EnableImageRecognition 关闭
- 图片下载失败 / 非受支持格式时优雅降级为纯文本

运行：python -m pytest tests -v -o "addopts="
"""
import base64
import sys
from pathlib import Path

import httpx
import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import group_message, private_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
)

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as plugin_tools  # noqa: E402

PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"
GROUP_ID = "200200"
SELF_ID = "10001"  # 与事件工厂默认 self_id 一致


# 1x1 透明 PNG（仅用于类型测试，不校验图片有效性）
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)
JPEG_BYTES = b'\xff\xd8\xff\xe0' + b'\x00' * 20 + b'\xff\xd9'
GIF_BYTES = b'GIF89a' + b'\x00' * 16
WEBP_BYTES = b'RIFF\x24\x00\x00\x00WEBP' + b'\x00' * 16
NOT_IMAGE_BYTES = b'hello this is not an image'

IMAGE_URL = 'https://qpic.example.com/pic/1.jpg'

# 公网 IP：stub DNS 解析器统一返回，避免单测发起真实 DNS 请求（SSRF 校验需要解析主机）
PUBLIC_IP = '93.184.216.34'


@pytest.fixture(autouse=True)
def _stub_dns_resolver(monkeypatch):
    async def _resolver(host: str) -> list[str]:
        return [PUBLIC_IP]
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _resolver)


# ==================== fetch_image_data_url / _sniff_image_mime 单元测试 ====================


def _transport_for(content: bytes, status_code: int = 200, content_type: str = 'image/png') -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status_code, content=content, headers={'content-type': content_type})
    return httpx.MockTransport(handler)


@pytest.mark.parametrize("content,mime", [
    (PNG_BYTES, 'image/png'),
    (JPEG_BYTES, 'image/jpeg'),
    (GIF_BYTES, 'image/gif'),
    (WEBP_BYTES, 'image/webp'),
])
async def test_fetch_image_returns_data_url(content, mime):
    """HTTP 下载成功且为受支持图片时，返回含 data_url 的视觉信息"""
    vision = await plugin_tools.fetch_image_data_url(
        IMAGE_URL, transport=_transport_for(content, content_type=mime))
    assert vision is not None
    assert vision['type'] == 'image'
    assert vision['mime_type'] == mime
    assert vision['size'] == len(content)
    assert vision['source'] == IMAGE_URL
    # data URL 应可还原出原始字节
    data_url = vision['data_url']
    assert data_url.startswith(f'data:{mime};base64,')
    assert base64.b64decode(data_url.split(',', 1)[1]) == content


async def test_fetch_image_local_file(tmp_path):
    """本地文件路径直接读取，无需网络"""
    p = tmp_path / 'pic.png'
    p.write_bytes(PNG_BYTES)
    vision = await plugin_tools.fetch_image_data_url(str(p))
    assert vision is not None
    assert vision['mime_type'] == 'image/png'
    assert vision['source'] == str(p)


async def test_fetch_image_rejects_non_image():
    """魔数不匹配（非受支持图片格式）时返回 None"""
    assert await plugin_tools.fetch_image_data_url(
        IMAGE_URL, transport=_transport_for(NOT_IMAGE_BYTES, content_type='text/plain')) is None


async def test_fetch_image_rejects_oversized():
    """超过大小上限（>5MiB）时返回 None"""
    huge = b'\x89PNG\r\n\x1a\n' + b'\x00' * (5 * 1024 * 1024 + 10)
    assert await plugin_tools.fetch_image_data_url(
        IMAGE_URL, transport=_transport_for(huge)) is None


async def test_fetch_image_rejects_error_status():
    """HTTP 4xx/5xx 时返回 None"""
    assert await plugin_tools.fetch_image_data_url(
        IMAGE_URL, transport=_transport_for(b'nope', status_code=404)) is None


async def test_fetch_image_empty_and_blank_source():
    """空字节 / 空白 source 返回 None，不抛异常"""
    assert await plugin_tools.fetch_image_data_url(
        IMAGE_URL, transport=_transport_for(b'')) is None
    assert await plugin_tools.fetch_image_data_url('   ') is None
    assert await plugin_tools.fetch_image_data_url('') is None


@pytest.mark.parametrize("content,mime", [
    (PNG_BYTES, 'image/png'),
    (JPEG_BYTES, 'image/jpeg'),
    (GIF_BYTES, 'image/gif'),
    (WEBP_BYTES, 'image/webp'),
    (NOT_IMAGE_BYTES, None),
])
async def test_sniff_image_mime(content, mime):
    """按魔数推断 MIME 类型，无法识别返回 None"""
    assert plugin_tools._sniff_image_mime(content) == mime


# ==================== 对话链路测试 ====================


def _harness() -> PluginTestHarness:
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


def _patch_image_transport(monkeypatch, plugin, content: bytes):
    """让插件 main 实际使用的 tools 模块走 MockTransport 下载图片，
    并 stub DNS 解析器避免真实 DNS 请求（SSRF 主机校验需要解析主机）"""
    main_module = sys.modules[plugin.__class__.__module__]
    monkeypatch.setattr(
        main_module.tools, '_default_http_transport',
        _transport_for(content, content_type='image/png'))

    async def _resolver(host: str) -> list[str]:
        return [PUBLIC_IP]
    monkeypatch.setattr(main_module.tools, '_dns_resolver', _resolver)


async def test_private_image_attached_as_vision_message(tmp_path, monkeypatch):
    """私聊图片+文字：模型收到多模态 content（text + image_url data URL）"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        _patch_image_transport(monkeypatch, plugin, PNG_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('图片里是一只猫'), 'stop'))])
        plugin._default_client = client

        await h.inject(private_message(
            '这是什么',
            user_id=ROOT_QQ,
            message=[
                {'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}},
                {'type': 'text', 'data': {'text': '这是什么'}},
            ],
        ))
        await h.settle()

        h.assert_api('send_private_msg').with_text('图片里是一只猫')

        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        assert isinstance(user_msg['content'], list)
        parts = user_msg['content']
        assert parts[0] == {'type': 'text', 'text': '这是什么'}
        image_parts = [p for p in parts if p['type'] == 'image_url']
        assert len(image_parts) == 1
        assert image_parts[0]['image_url']['url'].startswith('data:image/png;base64,')

        # 会话历史同样保存多模态内容
        session = plugin.data['data']['user_conversations'][ROOT_QQ]
        user_entry = next(m for m in session if m['role'] == 'user')
        assert any(
            isinstance(p, dict) and p.get('type') == 'image_url'
            for p in user_entry['content']
        )


async def test_image_only_uses_default_text(tmp_path, monkeypatch):
    """仅发图片无文字时，文本部分回退为默认说明，仍携带图片"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        _patch_image_transport(monkeypatch, plugin, PNG_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('收到图片'), 'stop'))])
        plugin._default_client = client

        await h.inject(private_message(
            user_id=ROOT_QQ,
            message=[{'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}}],
        ))
        await h.settle()

        h.assert_api('send_private_msg').with_text('收到图片')
        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        assert user_msg['content'][0]['text'] == '用户发送了图片，请查看随附图片。'
        assert any(p['type'] == 'image_url' for p in user_msg['content'])


async def test_group_at_bot_with_image(tmp_path, monkeypatch):
    """群聊 @机器人 + 图片：MustAtBot 校验通过后进入视觉对话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        _patch_image_transport(monkeypatch, plugin, PNG_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('看到了'), 'stop'))])
        plugin._default_client = client

        await h.inject(group_message(
            '看看这个',
            group_id=GROUP_ID,
            user_id=ROOT_QQ,
            self_id=SELF_ID,
            raw_message=f'[CQ:at,qq={SELF_ID}] 看看这个',
            message=[
                {'type': 'at', 'data': {'qq': SELF_ID}},
                {'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}},
                {'type': 'text', 'data': {'text': ' 看看这个'}},
            ],
        ))
        await h.settle()

        h.assert_api('send_group_msg').with_text('看到了')
        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        assert user_msg['content'][0]['text'] == '看看这个'
        assert any(p['type'] == 'image_url' for p in user_msg['content'])


async def test_image_recognition_disabled_ignores_image(tmp_path, monkeypatch):
    """EnableImageRecognition=False：图片被忽略，内容保持纯文本字符串"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        plugin.config['EnableImageRecognition'] = False
        # 即便注入 MockTransport，关闭后也不应尝试下载
        _patch_image_transport(monkeypatch, plugin, PNG_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('好的'), 'stop'))])
        plugin._default_client = client

        await h.inject(private_message(
            '这是一张图',
            user_id=ROOT_QQ,
            message=[
                {'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}},
                {'type': 'text', 'data': {'text': '这是一张图'}},
            ],
        ))
        await h.settle()

        h.assert_api('send_private_msg').with_text('好的')
        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        # 关闭识别时 content 为纯文本字符串，不含图片段
        assert isinstance(user_msg['content'], str)
        assert user_msg['content'] == '这是一张图'


async def test_image_download_failure_degrades_to_text(tmp_path, monkeypatch):
    """图片下载失败（非受支持格式）时优雅降级：仅文本进入模型，不中断对话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        _patch_image_transport(monkeypatch, plugin, NOT_IMAGE_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('好的'), 'stop'))])
        plugin._default_client = client

        await h.inject(private_message(
            '看看这个',
            user_id=ROOT_QQ,
            message=[
                {'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}},
                {'type': 'text', 'data': {'text': '看看这个'}},
            ],
        ))
        await h.settle()

        h.assert_api('send_private_msg').with_text('好的')
        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        assert isinstance(user_msg['content'], str)
        assert user_msg['content'] == '看看这个'


async def test_image_local_path_missing_ignored(tmp_path, monkeypatch):
    """图片段为不存在的本地路径时忽略，仅文本进入模型，不中断对话"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('好的'), 'stop'))])
        plugin._default_client = client

        missing = str(tmp_path / 'no_such.png')
        await h.inject(private_message(
            '看看这个',
            user_id=ROOT_QQ,
            message=[
                {'type': 'image', 'data': {'file': missing}},
                {'type': 'text', 'data': {'text': '看看这个'}},
            ],
        ))
        await h.settle()

        h.assert_api('send_private_msg').with_text('好的')
        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        assert user_msg['content'] == '看看这个'


async def test_multiple_images_all_attached(tmp_path, monkeypatch):
    """一条消息含多张图片时全部作为 image_url 段附加"""
    monkeypatch.chdir(tmp_path)
    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config['IsConfigured'] = True
        _patch_image_transport(monkeypatch, plugin, PNG_BYTES)
        client = FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage('两张都看到了'), 'stop'))])
        plugin._default_client = client

        await h.inject(private_message(
            '这两张图',
            user_id=ROOT_QQ,
            message=[
                {'type': 'image', 'data': {'file': IMAGE_URL, 'url': IMAGE_URL}},
                {'type': 'image', 'data': {'file': IMAGE_URL + '2', 'url': IMAGE_URL + '2'}},
                {'type': 'text', 'data': {'text': '这两张图'}},
            ],
        ))
        await h.settle()

        sent_messages = client.chat.completions.calls[0]['messages']
        user_msg = next(m for m in sent_messages if m['role'] == 'user')
        image_parts = [p for p in user_msg['content'] if p['type'] == 'image_url']
        assert len(image_parts) == 2
