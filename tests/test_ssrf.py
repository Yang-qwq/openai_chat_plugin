# -*- coding: utf-8 -*-
"""SSRF 防护测试：主机校验、DNS 解析判定与重定向逐跳复核。

覆盖：
- ``_classify_ip``：各受保护地址段的分类（回环/内网/链路本地/保留/未指定/组播/非全局）
- ``_check_protected_host``：IP 字面量、DNS 解析（含混合记录）、fail-closed、配置放行
- ``create_http_request``：受保护地址在发请求前被拦截；重定向逐跳复核；
  重定向方法降级与保持；重定向上限；非法重定向目标
- ``fetch_image_data_url``：图片下载链路共用同一道主机校验闸门
- 对话链路：插件按运营配置（``AllowLoopbackIps`` / ``AllowLocalNetworkIps``）
  把策略注入工具调用；模型传入的同名参数被忽略

用 httpx.MockTransport + 注入 DNS 解析器 stub 隔离真实网络。

运行：python -m pytest tests -v -o "addopts="
"""
import base64
import json
import sys
from pathlib import Path

import httpx
import pytest
from ncatbot.testing import PluginTestHarness
from ncatbot.testing.factories.qq import private_message

from fake_openai import (
    FakeChoice,
    FakeMessage,
    FakeOpenAIClient,
    FakeResponse,
    FakeToolCall,
)

pytestmark = pytest.mark.asyncio

PLUGINS_DIR = Path(__file__).resolve().parents[2]
PLUGIN_NAME = "OpenAIChatPlugin"
ROOT_QQ = "123456"

PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

import tools as plugin_tools  # noqa: E402

# 1x1 透明 PNG
PNG_BYTES = base64.b64decode(
    'iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=='
)
# 公网 IP：作为 stub 解析器的默认返回（所有域名都「解析」到公网，模拟正常情况）
PUBLIC_IP = '93.184.216.34'


def _payload(result: str) -> dict:
    return json.loads(result)


def _resolver(ips):
    """构造 DNS 解析器 stub：所有主机都返回给定的 IP 列表"""
    async def _resolve(host: str) -> list[str]:
        return list(ips)
    return _resolve


def _public_resolver():
    return _resolver([PUBLIC_IP])


def _transport(handler) -> httpx.MockTransport:
    return httpx.MockTransport(handler)


@pytest.fixture(autouse=True)
def _stub_dns(monkeypatch):
    """默认把 DNS 解析 stub 为公网 IP，避免单测发起真实 DNS 请求"""
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _public_resolver())


# ==================== _classify_ip：地址段分类 ====================

@pytest.mark.parametrize('ip,expected', [
    # 回环
    ('127.0.0.1', '回环地址'),
    ('127.1.2.3', '回环地址'),
    ('::1', '回环地址'),
    ('::ffff:127.0.0.1', '回环地址'),  # IPv4-mapped 按 IPv4 判定，防绕过
    # 内网
    ('10.1.2.3', '内网地址'),
    ('172.16.0.1', '内网地址'),
    ('192.168.1.1', '内网地址'),
    ('fc00::1', '内网地址'),
    ('fd12:3456::1', '内网地址'),
    # 链路本地（含云元数据 169.254.169.254）——一律拦截，无配置项
    ('169.254.169.254', '链路本地地址（含云元数据）'),
    ('169.254.0.1', '链路本地地址（含云元数据）'),
    ('fe80::1', '链路本地地址（含云元数据）'),
    # 未指定 / 组播
    ('0.0.0.0', '未指定/组播地址'),
    ('::', '未指定/组播地址'),
    ('224.0.0.1', '未指定/组播地址'),
    ('ff02::1', '未指定/组播地址'),
    # 保留段
    ('240.0.0.1', '保留地址段'),
    ('255.255.255.255', '保留地址段'),
    # 非全局可路由（CGNAT 等）
    ('100.64.0.1', '非全局可路由地址'),
    # 公网放行
    ('8.8.8.8', None),
    (PUBLIC_IP, None),
    ('::ffff:8.8.8.8', None),
    ('2606:4700:4700::1111', None),
    # 非法输入
    ('example.com', None),
    ('', None),
])
async def test_classify_ip(ip, expected):
    """各类 IP 命中对应的受保护类别；公网与非法输入返回 None"""
    assert plugin_tools._classify_ip(ip) == expected


# ==================== _check_protected_host：主机校验 ====================

async def test_check_protected_host_ip_literals():
    """IP 字面量主机直接判定，无需 DNS"""
    assert await plugin_tools._check_protected_host('http://127.0.0.1:8080/admin') == (True, '回环地址')
    assert await plugin_tools._check_protected_host('http://[::1]:8080/') == (True, '回环地址')
    hit, reason = await plugin_tools._check_protected_host('http://169.254.169.254/latest/meta-data/')
    assert hit is True
    assert '链路本地' in reason
    assert await plugin_tools._check_protected_host('http://10.0.0.1/x') == (True, '内网地址')
    # 公网地址不命中
    assert await plugin_tools._check_protected_host('http://8.8.8.8/dns') == (False, '')


async def test_check_protected_host_uses_dns_resolution(monkeypatch):
    """域名主机经 DNS 解析后逐条判定"""
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _resolver(['10.0.0.1']))
    assert await plugin_tools._check_protected_host('https://intranet.example.com/') == (True, '内网地址')

    # 任一解析记录命中即拒绝（防止目标同时返回公网+内网记录）
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _resolver([PUBLIC_IP, '127.0.0.1']))
    assert await plugin_tools._check_protected_host('https://mixed.example.com/') == (True, '回环地址')

    # 全部为公网记录则放行
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _resolver([PUBLIC_IP]))
    assert await plugin_tools._check_protected_host('https://public.example.com/') == (False, '')


async def test_check_protected_host_dns_failure_is_blocked(monkeypatch):
    """DNS 解析失败一律拒绝（fail-closed），不冒险放行"""
    async def _empty(host):
        return []
    monkeypatch.setattr(plugin_tools, '_dns_resolver', _empty)
    hit, reason = await plugin_tools._check_protected_host('https://unresolvable.example.com/')
    assert hit is True
    assert 'DNS' in reason


async def test_check_protected_host_allow_flags():
    """AllowLoopbackIps / AllowLocalNetworkIps 放行对应类别；其余段一律不可放行"""
    # 默认拒绝回环与内网
    hit, reason = await plugin_tools._check_protected_host('http://127.0.0.1/x')
    assert hit is True and reason == '回环地址'
    hit, reason = await plugin_tools._check_protected_host('http://192.168.1.1/x')
    assert hit is True and reason == '内网地址'
    # 显式放行回环
    hit, _ = await plugin_tools._check_protected_host(
        'http://127.0.0.1/x', allow_loopback_ips=True)
    assert hit is False
    hit, _ = await plugin_tools._check_protected_host(
        'http://[::1]/x', allow_loopback_ips=True)
    assert hit is False
    # 显式放行内网
    hit, _ = await plugin_tools._check_protected_host(
        'http://10.0.0.1/x', allow_local_network_ips=True)
    assert hit is False
    # 回环放行不等于内网放行（反之亦然）
    hit, _ = await plugin_tools._check_protected_host(
        'http://10.0.0.1/x', allow_loopback_ips=True)
    assert hit is True
    hit, _ = await plugin_tools._check_protected_host(
        'http://127.0.0.1/x', allow_local_network_ips=True)
    assert hit is True
    # 链路本地（云元数据）即使两个开关全开也拒绝
    hit, _ = await plugin_tools._check_protected_host(
        'http://169.254.169.254/latest/meta-data/',
        allow_loopback_ips=True, allow_local_network_ips=True)
    assert hit is True
    # 保留 / 未指定 / 组播同样不可放行
    hit, _ = await plugin_tools._check_protected_host(
        'http://240.0.0.1/x', allow_loopback_ips=True, allow_local_network_ips=True)
    assert hit is True


# ==================== create_http_request：工具链路 ====================

async def test_http_request_to_loopback_blocked_without_connection(monkeypatch):
    """回环地址在建立连接前被拦截，且错误文案不泄露具体类别"""
    requested = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        return httpx.Response(200, text='must not happen')

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'http://127.0.0.1:8080/admin', content_format='text',
        transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert payload['message'] == '请求失败：目标地址被禁止访问'
    assert requested == []  # 请求从未发出


async def test_http_request_to_metadata_ip_blocked():
    """云元数据地址被硬拦截（即使两个放行开关都开）"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError('metadata host must not be requested')

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'http://169.254.169.254/latest/meta-data/iam/security-credentials/',
        content_format='text', transport=_transport(handler),
        allow_loopback_ips=True, allow_local_network_ips=True))
    assert payload['status'] == 'error'


async def test_http_request_to_public_host_allowed():
    """公网地址正常请求"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text='hello', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/', content_format='text', transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert payload['data']['content'] == 'hello'


async def test_http_request_redirect_to_protected_host_blocked():
    """公网 URL 302 到内网地址：第二跳主机校验拦截，请求不发出"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.host == 'example.com':
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/'})
        raise AssertionError(f'protected host must not be requested: {request.url}')

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/redirect', content_format='text',
        transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert payload['message'] == '请求失败：目标地址被禁止访问'
    assert seen == ['https://example.com/redirect']


async def test_http_request_follows_redirect_to_public_host():
    """重定向到公网主机时正常跟随，并使用最终 URL 作为图片来源"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == '/a':
            return httpx.Response(302, headers={'location': 'https://cdn.example.com/b'})
        return httpx.Response(200, content=PNG_BYTES, headers={'content-type': 'image/png'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/a', content_format='image', transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert payload['data']['source'] == 'https://cdn.example.com/b'
    assert seen == ['https://example.com/a', 'https://cdn.example.com/b']

    # 相对路径重定向也能解析为绝对 URL
    seen.clear()

    def rel_handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path == '/a':
            return httpx.Response(302, headers={'location': 'b?x=1'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/a', content_format='text', transport=_transport(rel_handler)))
    assert payload['status'] == 'success'
    assert seen == ['https://example.com/a', 'https://example.com/b?x=1']


async def test_http_request_redirect_to_non_http_scheme_rejected():
    """重定向到 file:// 等非 http(s) 协议被拒绝"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/a':
            return httpx.Response(302, headers={'location': 'file:///etc/passwd'})
        raise AssertionError('must not request non-http target')

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/a', content_format='text', transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert '重定向目标' in payload['message']


async def test_http_request_redirect_loop_rejected():
    """重定向次数超过上限被拒绝（不再无限跟随）"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(302, headers={'location': 'https://example.com/loop'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/loop', content_format='text', transport=_transport(handler)))
    assert payload['status'] == 'error'
    assert payload['message'] == '请求失败：重定向次数过多'


async def test_http_request_redirect_downgrades_post_to_get():
    """301/302/303 把 POST 降级为 GET 并丢弃请求体（与 httpx 默认行为一致）"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == 'POST':
            return httpx.Response(302, headers={'location': 'https://example.com/done'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'POST', 'https://example.com/post', content_format='text',
        data={'a': 1}, transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert seen[0][0] == 'POST'
    assert seen[1] == ('GET', '/done', b'')


async def test_http_request_redirect_307_keeps_post_body():
    """307/308 保持原方法与请求体"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.url.path == '/post':
            return httpx.Response(307, headers={'location': 'https://example.com/again'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'POST', 'https://example.com/post', content_format='text',
        data={'a': 1}, transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert seen[1][0] == 'POST'
    assert json.loads(seen[1][2]) == {'a': 1}


async def test_http_request_redirect_strips_cross_origin_credentials():
    """跨源重定向不携带 Authorization/Cookie，Host 按目标主机重建（与 httpx 一致）"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((str(request.url), dict(request.headers)))
        if request.url.host == 'example.com':
            return httpx.Response(302, headers={'location': 'https://cdn.example.com/x'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/a', content_format='text',
        headers={'Authorization': 'Bearer SECRET', 'Cookie': 'sid=1'},
        transport=_transport(handler)))
    assert payload['status'] == 'success'
    first_url, first_headers = seen[0]
    second_url, second_headers = seen[1]
    assert first_url == 'https://example.com/a'
    assert second_url == 'https://cdn.example.com/x'
    assert first_headers.get('authorization') == 'Bearer SECRET'
    assert first_headers.get('cookie') == 'sid=1'
    # 跨源：凭据不得发给第三方；Host 由 httpx 重新生成
    assert 'authorization' not in second_headers
    assert 'cookie' not in second_headers
    assert second_headers.get('host') == 'cdn.example.com'


async def test_http_request_redirect_same_origin_keeps_authorization():
    """同源重定向（含 http→https 同主机）保留 Authorization，Cookie 仍被清理"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, str(request.url), dict(request.headers)))
        if request.url.path == '/a':
            return httpx.Response(302, headers={'location': 'https://example.com/b'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'http://example.com/a', content_format='text',
        headers={'Authorization': 'Bearer SECRET', 'Cookie': 'sid=1'},
        transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert seen[1][1] == 'https://example.com/b'
    assert seen[1][2].get('authorization') == 'Bearer SECRET'
    assert 'cookie' not in seen[1][2]


async def test_http_request_303_downgrades_put_to_get():
    """303 对任意非 HEAD 方法都降级为 GET 并丢弃请求体（与 httpx 一致）"""
    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.content))
        if request.method == 'PUT':
            return httpx.Response(303, headers={'location': 'https://example.com/done'})
        return httpx.Response(200, text='ok', headers={'content-type': 'text/plain'})

    payload = _payload(await plugin_tools.create_http_request(
        'PUT', 'https://example.com/put', content_format='text',
        data={'a': 1}, transport=_transport(handler)))
    assert payload['status'] == 'success'
    assert seen[0][0] == 'PUT'
    assert seen[1] == ('GET', '/done', b'')


async def test_http_request_unified_network_error_message():
    """网络异常文案统一为「请求失败…」，不区分超时/连接失败（防止探测 oracle）"""
    def timeout_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout('slow', request=request)

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/slow', content_format='text',
        transport=_transport(timeout_handler)))
    assert payload['status'] == 'error'
    assert '超时' in payload['message'] and payload['message'].startswith('请求失败')

    def error_handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError('boom', request=request)

    payload = _payload(await plugin_tools.create_http_request(
        'GET', 'https://example.com/err', content_format='text',
        transport=_transport(error_handler)))
    assert payload['status'] == 'error'
    assert payload['message'] == '请求失败：无法访问目标地址'


# ==================== fetch_image_data_url：图片下载链路 ====================

async def test_fetch_image_blocked_source():
    """图片 URL 指向回环地址时被拒绝（返回 None），不发起请求"""
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError('protected host must not be requested')

    assert await plugin_tools.fetch_image_data_url(
        'http://127.0.0.1:8080/secret.png', transport=_transport(handler)) is None


async def test_fetch_image_redirect_to_protected_host():
    """图片 URL 302 到内网地址时被拒绝"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == 'cdn.example.com':
            return httpx.Response(302, headers={'location': 'http://169.254.169.254/x.png'})
        raise AssertionError('protected host must not be requested')

    assert await plugin_tools.fetch_image_data_url(
        'https://cdn.example.com/pic.png', transport=_transport(handler)) is None


async def test_fetch_image_redirect_to_public_host():
    """图片重定向到公网主机时正常跟随"""
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == '/pic.png':
            return httpx.Response(302, headers={'location': 'https://cdn.example.com/real.png'})
        return httpx.Response(200, content=PNG_BYTES, headers={'content-type': 'image/png'})

    vision = await plugin_tools.fetch_image_data_url(
        'https://example.com/pic.png', transport=_transport(handler))
    assert vision is not None
    assert vision['source'] == 'https://example.com/pic.png'
    assert vision['data_url'].startswith('data:image/png;base64,')


async def test_fetch_image_allow_flags():
    """AllowLocalNetworkIps 放行后可下载内网图片"""
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=PNG_BYTES, headers={'content-type': 'image/png'})

    vision = await plugin_tools.fetch_image_data_url(
        'http://10.0.0.5/pic.png', transport=_transport(handler),
        allow_local_network_ips=True)
    assert vision is not None


# ==================== 对话链路：配置注入与模型参数隔离 ====================

def _harness() -> PluginTestHarness:
    return PluginTestHarness(
        plugin_names=[PLUGIN_NAME],
        plugins_dir=PLUGINS_DIR,
    )


def _patch_tools(monkeypatch, plugin, *, ips=(PUBLIC_IP,), handler=None):
    """向插件 main 实际使用的 tools 模块注入 DNS stub 与 MockTransport"""
    main_module = sys.modules[plugin.__class__.__module__]
    monkeypatch.setattr(main_module.tools, '_dns_resolver', _resolver(list(ips)))
    if handler is not None:
        monkeypatch.setattr(
            main_module.tools, '_default_http_transport', httpx.MockTransport(handler))


async def test_plugin_config_gates_loopback_by_default(tmp_path, monkeypatch):
    """默认配置下模型请求 127.0.0.1 被拒绝；工具结果含被拦截提示"""
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError('protected host must not be requested')

    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowWebRequests"] = True  # 允许出网，但回环默认仍被拦截
        _patch_tools(monkeypatch, plugin, ips=['127.0.0.1'], handler=handler)
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "create_http_request",
                    '{"method": "GET", "url": "http://127.0.0.1:8080/admin", '
                    '"content_format": "text"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage("目标地址被拒绝了"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("帮我看看本地服务", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("目标地址被拒绝了")
        session = plugin._sessions.load("user_conversations", ROOT_QQ)
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "被禁止访问" in tool_entry["content"]


async def test_plugin_config_allow_loopback_opens_loopback(tmp_path, monkeypatch):
    """AllowLoopbackIps=True 时模型请求回环地址可正常完成"""
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url).startswith('http://127.0.0.1:8080/')
        return httpx.Response(200, text='local-ok', headers={'content-type': 'text/plain'})

    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowWebRequests"] = True
        plugin.config["AllowLoopbackIps"] = True  # 运营显式放行回环
        _patch_tools(monkeypatch, plugin, ips=['127.0.0.1'], handler=handler)
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "create_http_request",
                    '{"method": "GET", "url": "http://127.0.0.1:8080/admin", '
                    '"content_format": "text"}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage("本地服务返回 local-ok"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("帮我看看本地服务", user_id=ROOT_QQ))
        await h.settle()

        h.assert_api("send_private_msg").with_text("本地服务返回 local-ok")
        session = plugin._sessions.load("user_conversations", ROOT_QQ)
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert '"status": "success"' in tool_entry["content"]


async def test_plugin_ignores_model_supplied_policy_args(tmp_path, monkeypatch):
    """模型在工具参数里传入 allow_loopback_ips 等同名参数时被忽略，
    不能绕过运营配置（默认 AllowLoopbackIps=False → 请求仍被拦截）"""
    monkeypatch.chdir(tmp_path)

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError('protected host must not be requested')

    async with _harness() as h:
        plugin = h.get_plugin(PLUGIN_NAME)
        plugin.config["IsConfigured"] = True
        plugin.config["EnableBuiltinFunctionCalling"] = True
        plugin.config["AllowWebRequests"] = True
        _patch_tools(monkeypatch, plugin, ips=['127.0.0.1'], handler=handler)
        client = FakeOpenAIClient([
            FakeResponse(FakeChoice(FakeMessage(
                content=None,
                tool_calls=[FakeToolCall(
                    "call_1", "create_http_request",
                    '{"method": "GET", "url": "http://127.0.0.1:8080/admin", '
                    '"content_format": "text", "allow_loopback_ips": true}',
                )],
            ), "tool_calls")),
            FakeResponse(FakeChoice(FakeMessage("仍然被拒绝"), "stop")),
        ])
        plugin._default_client = client

        await h.inject(private_message("帮我看看本地服务", user_id=ROOT_QQ))
        await h.settle()

        # 模型注入的参数不能生效：请求仍被拦截，且流程不因重复关键字报错
        h.assert_api("send_private_msg").with_text("仍然被拒绝")
        session = plugin._sessions.load("user_conversations", ROOT_QQ)
        tool_entry = next(m for m in session if m["role"] == "tool")
        assert "被禁止访问" in tool_entry["content"]
