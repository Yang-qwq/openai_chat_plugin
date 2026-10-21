# -*- coding: utf-8 -*-
"""
Function Calling 工具代码实现

## 记忆工具

增删查功能，数据存储在工作空间的 `memory.json`。

v0.1.4+ 记忆数据结构（列表中的每一项是一个 dict）：
```python
[
    {
        'id': '<str:UUID>',
        'from_user': int,     # 产生该记忆的用户ID；0 表示“全局/未知来源”
        'from_group': int,    # 群ID；-1 表示私聊/全局/未知来源
        'create_time': 'str:ISO8601',  # 例如 '2024-06-01T12:00:00Z'
        'content': '这是第一条记忆'
    },
    ...
]
```
"""

import base64
import json
import os
import re
import uuid
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx
import markdownify
from ncatbot.api.qq import QQAPIClient
from ncatbot.event.qq import GroupMessageEvent, MessageEvent
from ncatbot.utils.logger import get_log

__all__ = [
    '_generate_tool_payload',
    '_generate_vision_tool_payload',
    'access_memory',
    'create_http_request',
    'extract_vision_payload',
    'get_environment_info',
    'get_group_info',
    'get_stranger_info',
    'get_system_time',
    'query_missing_message_context',
    'tools',
]

_log = get_log('openai_chat_plugin.tools')

# ---- HTTP 请求工具限制 ----
# 允许的 HTTP 方法（大小写不敏感）
ALLOWED_HTTP_METHODS = {'GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD', 'OPTIONS'}
# 允许的请求内容格式
ALLOWED_CONTENT_FORMATS = {'json', 'text', 'markdown', 'image'}
# 文本类响应最大字节数，防止模型上下文被超大网页撑爆
MAX_TEXT_RESPONSE_BYTES = 2 * 1024 * 1024  # 2 MiB
# 图片响应最大字节数（base64 后约膨胀 4/3）
MAX_IMAGE_RESPONSE_BYTES = 5 * 1024 * 1024  # 5 MiB
# 支持交给视觉模型读取的图片 MIME 类型
ALLOWED_IMAGE_MIME_TYPES = {'image/png', 'image/jpeg', 'image/gif', 'image/webp'}
# 请求超时（秒）
DEFAULT_HTTP_TIMEOUT = 15.0
MAX_HTTP_TIMEOUT = 60.0
# 默认 transport（None 表示使用 httpx 默认实现；测试中可注入 MockTransport）
_default_http_transport: httpx.AsyncBaseTransport | None = None

tools = [
    {
        'type': 'function',
        'function': {
            'name': 'access_memory',
            'description': 'Used for reading, writing and deleting memory data',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['add', 'query_by_regex', 'query_by_user_id', 'query_by_group_id', 'delete'],
                        'description': "The type of operation."
                    },
                    'content': {
                        'type': 'string',
                        'description': "When action is 'add', content is the memory content to be added; "
                                       "when action is 'query_by_regex', content is the regex for querying "
                                       "(optional, blank returns all memories, may cause performance issues); "
                                       "when action like 'query_by_(user|group)_id', content is the integer id; "
                                       "when action is 'delete', content is the specific memory id;"
                    }
                },
                'required': ['action']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_environment_info',
            'description': 'Get current chat environment',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': []
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_stranger_info',
            'description': 'Get user info by user ID (include user_id, nickname, sex, age etc.)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'user_id': {
                        'type': 'integer',
                        'description': 'The user ID of the stranger'
                    }
                },
                'required': ['user_id']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_group_info',
            'description': 'Get group information (include group_id, group_name, member_count etc.)',
            'parameters': {
                'type': 'object',
                'properties': {
                    'group_id': {
                        'type': 'integer',
                        'description': 'The group ID'
                    }
                },
                'required': ['group_id']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'query_missing_message_context',
            'description': 'Query the missing message context between single chat messages (only for group chats)',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': []
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_system_time',
            'description': 'Get system time in multiple formats',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': []
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'create_http_request',
            'description': 'Create an HTTP request and return the response. '
                           'Use content_format="image" to fetch an image and view it.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'content_format': {
                        'type': 'string',
                        'enum': ['json', 'text', 'markdown', 'image'],
                        'description': "The format of the content to be received in the request. "
                                       "Use 'image' to attach a fetched image to the conversation "
                                       "so it can be seen directly."
                    },
                    'method': {
                        'type': 'string',
                        'enum': sorted(ALLOWED_HTTP_METHODS),
                        'description': "HTTP method, e.g., 'GET', 'POST', etc."
                    },
                    'url': {
                        'type': 'string',
                        'description': "The URL to send the request to (http/https only)"
                    },
                    'data': {
                        'type': 'object',
                        'description': "Optional JSON data to send in the request body (for POST, PUT, etc.)"
                    },
                    'headers': {
                        'type': 'object',
                        'description': "Optional HTTP request headers"
                    },
                    'params': {
                        'type': 'object',
                        'description': "Optional URL query parameters"
                    },
                    'timeout': {
                        'type': 'number',
                        'description': f"Request timeout in seconds (default {DEFAULT_HTTP_TIMEOUT:g}, "
                                       f"max {MAX_HTTP_TIMEOUT:g})"
                    }
                },
                'required': ['method', 'url']
            }
        }
    }
]


def _parse_int_id(content: object) -> int | None:
    """将工具参数 content 解析为整数 ID（兼容 str / int，支持负数）。"""
    if isinstance(content, bool):
        return None
    if isinstance(content, int):
        return content
    if isinstance(content, str):
        stripped = content.strip()
        if not stripped:
            return None
        try:
            return int(stripped)
        except ValueError:
            return None
    return None


def _generate_tool_payload(status: str, message: str = '', data: Any = None) -> str:
    """生成 Function Calling 工具返回值的标准 JSON payload

    :param status: 结果状态，如 'success' 或 'error'
    :param message: 人类可读的消息文本
    :param data: 可选的附加数据
    :return: JSON 字符串
    """
    payload: dict[str, Any] = {'status': status, 'message': message}
    if data is not None:
        payload['data'] = data
    return json.dumps(payload, ensure_ascii=False)


def _generate_vision_tool_payload(
        content: bytes,
        mime_type: str,
        source: str = '',
        status_code: int | None = None,
) -> str:
    """生成图片类工具返回值的 JSON payload（供主流程转换为视觉消息）

    返回的 ``data.data_url`` 是可直接放进 OpenAI ``image_url`` 的 data URL，
    主流程会把它附加为一条带图片的 user 消息；写入工具历史时会去掉
    base64 正文，避免把超大字符串塞进上下文。

    :param content: 图片原始字节
    :param mime_type: 图片 MIME 类型，如 image/png
    :param source: 图片来源 URL
    :param status_code: HTTP 状态码（可选）
    :return: JSON 字符串
    """
    encoded = base64.b64encode(content).decode('ascii')
    data: dict[str, Any] = {
        'type': 'image',
        'mime_type': mime_type,
        'size': len(content),
        'source': source,
        'data_url': f'data:{mime_type};base64,{encoded}',
    }
    if status_code is not None:
        data['status_code'] = status_code
    return _generate_tool_payload('success', '已获取图片，将作为视觉输入附加到对话中', data)


def extract_vision_payload(tool_result: str) -> dict[str, Any] | None:
    """从工具返回值中提取视觉图片信息。

    :param tool_result: 工具返回的 JSON 字符串
    :return: 含 data_url / mime_type 等字段的 dict；不是图片 payload 时返回 None
    """
    try:
        payload = json.loads(tool_result)
    except (TypeError, ValueError):
        return None
    if not isinstance(payload, dict) or payload.get('status') != 'success':
        return None
    data = payload.get('data')
    if not isinstance(data, dict) or data.get('type') != 'image':
        return None
    data_url = data.get('data_url')
    if not isinstance(data_url, str) or not data_url.startswith('data:image/'):
        return None
    return data


def _vision_summary_payload(vision: dict[str, Any]) -> str:
    """生成写入工具历史的图片摘要 payload（不含 base64 正文）"""
    data = {
        'type': 'image',
        'mime_type': vision.get('mime_type'),
        'size': vision.get('size'),
        'source': vision.get('source'),
    }
    return _generate_tool_payload('success', '已获取图片，将作为视觉输入附加到对话中', data)


def access_memory(
        work_space: os.PathLike | str,
        action: str,
        content: str | int | None = None,
        from_user: int | None = None,
        from_group: int | None = None,
        **_extra: object,
) -> str:
    """记忆读取、写入工具

    注：本工具内记忆未做隔离是我的设计意图，不存在任何安全问题，因为：

    1. 记忆内容来源于用户输入，用户可以控制自己的输入内容，因此不存在隐私泄露风险。
    2. 记忆操作仅限于增删查，不涉及执行任何代码或外部命令，因此不存在代码注入风险。

    :param work_space: 工作空间对象或路径
    :param action: 操作类型，支持 add / query_by_regex / query_by_user_id / query_by_group_id / delete
    :param content: add 时为记忆正文；query_by_regex 时为正则（省略或空白则返回全部）；query_by_*_id 时为整数 ID；delete 时为要删除的记忆 ID
    :param from_user: 插件注入，用户ID
    :param from_group: 插件注入，群ID
    :return: str, json字符串，包含操作结果
    """
    # 打开记忆文件
    memory_file = os.path.join(work_space, 'memory.json')
    if not os.path.exists(memory_file):
        _log.debug('记忆文件不存在，创建新的记忆文件')
        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump([], f)

    # 读取现有记忆数据
    with open(memory_file, 'r', encoding='utf-8') as f:
        memory_data = json.load(f)

    # 根据操作类型执行相应的逻辑
    # 添加记忆：生成新的ID，添加到记忆数据中，并写回文件
    if action == 'add':
        if not isinstance(content, str) or not content.strip():
            return _generate_tool_payload('error', '请提供非空的 content 字符串用于添加记忆')

        # 兼容：如果主程序没有注入来源，则写入“未知来源/全局”标记。
        new_id = str(uuid.uuid4())
        now_iso = datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace('+00:00', 'Z')
        new_memory = {
            'id': new_id,
            'from_user': from_user if isinstance(from_user, int) else 0,
            'from_group': from_group if isinstance(from_group, int) else -1,
            'create_time': now_iso,
            'content': content
        }
        memory_data.append(new_memory)
        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump(memory_data, f, ensure_ascii=False, indent=2)
        return _generate_tool_payload('success', '记忆添加成功')

    # 查询记忆：根据正则表达式过滤记忆内容，返回匹配的记忆列表
    elif action == 'query_by_regex':
        all_items = [item for item in memory_data if isinstance(item, dict)]

        if isinstance(content, str) and content.strip():
            try:
                pattern = re.compile(content.strip(), re.IGNORECASE)
            except re.error as exc:
                return _generate_tool_payload('error', f'无效的正则表达式: {exc}')

            filtered_memory = [
                item for item in all_items
                if isinstance(item.get('content'), str) and pattern.search(item['content'])
            ]
        else:
            filtered_memory = all_items

        return _generate_tool_payload('success', '', filtered_memory)

    # 查询记忆：根据用户ID过滤记忆
    elif action == 'query_by_user_id':
        user_id = _parse_int_id(content)
        if user_id is None:
            return _generate_tool_payload('error', '请提供一个有效的用户 ID（整数）')
        filtered_memory = [
            item for item in memory_data
            if isinstance(item, dict) and item.get('from_user') == user_id
        ]
        return _generate_tool_payload('success', '', filtered_memory)

    # 查询记忆：根据群组ID过滤记忆
    elif action == 'query_by_group_id':
        group_id = _parse_int_id(content)
        if group_id is None:
            return _generate_tool_payload('error', '请提供一个有效的群组 ID（整数）')
        filtered_memory = [
            item for item in memory_data
            if isinstance(item, dict) and item.get('from_group') == group_id
        ]
        return _generate_tool_payload('success', '', filtered_memory)

    # 删除记忆：根据 ID 删除记忆
    elif action == 'delete':
        if not isinstance(content, str):
            return _generate_tool_payload('error', '请提供要删除的记忆 ID')

        target_id = content.strip()
        if not target_id:
            return _generate_tool_payload('error', '请提供要删除的记忆 ID')

        new_memory_data = [
            item for item in memory_data
            if not (isinstance(item, dict) and item.get('id') == target_id)
        ]

        if len(new_memory_data) == len(memory_data):
            return _generate_tool_payload('error', '未找到要删除的记忆')

        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump(new_memory_data, f, ensure_ascii=False, indent=2)

        return _generate_tool_payload('success', '记忆删除成功')
    return _generate_tool_payload('error', '无效的操作类型')


async def get_environment_info(event: MessageEvent) -> str:
    """获取当前聊天环境信息

    :param event: MessageEvent 消息事件
    :return:
    """
    # 假定group_id不存在
    group_id = None

    # 如果是群消息，则提取group_id
    if isinstance(event, GroupMessageEvent):
        group_id = event.group_id

    environment = {
        'user_id': event.user_id,
        'group_id': group_id,
        'message_id': event.message_id,
        # 'message_type': event.message_type
    }

    return _generate_tool_payload('success', '', environment)


def _to_jsonable(data: Any) -> Any:
    """将 API 返回对象转换为可 JSON 序列化的结构

    v5 的查询 API 返回 pydantic model，需经 model_dump() 转换。

    :param data: API 返回数据（pydantic model 或原生结构）
    :return: 可 JSON 序列化的数据
    """
    return data.model_dump() if hasattr(data, 'model_dump') else data


async def get_stranger_info(api: QQAPIClient, user_id: int) -> str:
    """获取用户信息

    :param api: QQ 平台 API 客户端（self.api.qq）
    :param user_id: 用户ID
    :return: str, json字符串，包含用户信息
    """
    data = await api.query.get_stranger_info(user_id)

    return _generate_tool_payload('success', '', _to_jsonable(data))


async def get_group_info(api: QQAPIClient, group_id: int) -> str:
    """获取群聊信息

    :param api: QQ 平台 API 客户端（self.api.qq）
    :param group_id: 群ID
    :return: str, json字符串，包含群聊信息
    """
    data = await api.query.get_group_info(group_id)

    return _generate_tool_payload('success', '', _to_jsonable(data))


async def query_missing_message_context(temp_history_messages_kv: dict, group_id: int) -> str:
    """查询忽略的消息上下文（仅限群聊）

    :param temp_history_messages_kv: 外部对话临时存储
    :param group_id: 群ID
    :return: str, json字符串
    """
    try:
        missing_context = temp_history_messages_kv[str(group_id)]
    except Exception as e:
        return _generate_tool_payload('error', f'查询消息上下文时出错: {e}')
    return _generate_tool_payload('success', f'已获取{len(missing_context)}条消息', missing_context)

async def get_system_time() -> str:
    """获取多种格式的系统时间

    :return: str, json字符串，包含多种格式的系统时间
    """
    now = datetime.now(timezone.utc)

    return _generate_tool_payload('success', '', {
        'timestamp': now.timestamp(),
        'iso8601': now.isoformat().replace('+00:00', 'Z'),
        'rfc2822': now.strftime('%a, %d %b %Y %H:%M:%S GMT')
    })

def _normalize_http_method(method: object) -> str | None:
    """规范化并校验 HTTP 方法；非法时返回 None。"""
    if not isinstance(method, str):
        return None
    normalized = method.strip().upper()
    return normalized if normalized in ALLOWED_HTTP_METHODS else None


def _validate_http_url(url: object) -> tuple[str | None, str | None]:
    """校验请求 URL，返回 (规范化 URL, 错误信息)。"""
    if not isinstance(url, str) or not url.strip():
        return None, '请提供非空的 url'
    normalized = url.strip()
    parsed = urlparse(normalized)
    if parsed.scheme not in ('http', 'https') or not parsed.netloc:
        return None, '仅支持 http/https 协议的完整 URL'
    if parsed.username or parsed.password:
        return None, 'URL 中不允许携带用户名/密码'
    return normalized, None


def _decode_http_text(response: httpx.Response, content: bytes) -> str:
    """按响应 charset 解码文本（缺少 charset 时回退 utf-8）。"""
    encoding = response.charset_encoding or 'utf-8'
    try:
        return content.decode(encoding, errors='replace')
    except LookupError:
        return content.decode('utf-8', errors='replace')


def _http_message(response: httpx.Response) -> str:
    """构造 `200 OK` 形式的状态描述。"""
    reason = response.reason_phrase or ''
    return f'{response.status_code} {reason}'.strip()


def _markdownify_html(text: str) -> str:
    """尝试把 HTML 转为 Markdown；失败时抛出异常由调用方处理。"""
    return markdownify.markdownify(text, heading_style='ATX')


async def create_http_request(
        method: str,
        url: str,
        content_format: str = 'text',
        data: Any = None,
        headers: dict | None = None,
        params: dict | None = None,
        timeout: float | None = None,
        transport: httpx.AsyncBaseTransport | None = None,
        **_extra: Any,
) -> str:
    """创建 HTTP 请求并返回适合模型阅读的 JSON payload

    - ``content_format``：json / text / markdown / image
    - ``image`` 模式下会返回 data URL，主流程会将其转换为视觉消息附加到对话
    - 文本/网页响应默认限制 2 MiB，图片限制 5 MiB
    - 所有网络异常、参数错误都会转为 ``status: error`` 的 payload，不向上抛异常

    :param method: HTTP 方法，如 'GET'、'POST'
    :param url: 请求 URL（仅 http/https）
    :param content_format: 期望的响应内容格式
    :param data: 可选的 JSON 请求体（dict/list）
    :param headers: 可选的请求头
    :param params: 可选的 URL 查询参数
    :param timeout: 超时秒数（默认 15，最大 60）
    :param transport: 可选的自定义 httpx transport（测试用）
    :param _extra: 忽略模型多传的未知参数
    :return: json 字符串
    """
    normalized_method = _normalize_http_method(method)
    if normalized_method is None:
        return _generate_tool_payload('error', f'不支持的 HTTP 方法: {method}')

    normalized_url, url_error = _validate_http_url(url)
    if url_error:
        return _generate_tool_payload('error', url_error)

    normalized_format = (content_format or 'text').strip().lower() if isinstance(content_format, str) else ''
    if normalized_format not in ALLOWED_CONTENT_FORMATS:
        return _generate_tool_payload(
            'error', f'不支持的 content_format: {content_format}',
            {'supported': sorted(ALLOWED_CONTENT_FORMATS)})

    if data is not None and not isinstance(data, (dict, list)):
        return _generate_tool_payload('error', 'data 必须是 JSON 对象或数组')
    if headers is not None and not isinstance(headers, dict):
        return _generate_tool_payload('error', 'headers 必须是对象')
    if params is not None and not isinstance(params, dict):
        return _generate_tool_payload('error', 'params 必须是对象')

    try:
        request_timeout = DEFAULT_HTTP_TIMEOUT if timeout is None else float(timeout)
    except (TypeError, ValueError):
        return _generate_tool_payload('error', 'timeout 必须是数字')
    request_timeout = max(1.0, min(request_timeout, MAX_HTTP_TIMEOUT))

    # 统一转成字符串键值，避免模型传入非字符串导致 httpx 报错
    request_headers = {str(k): str(v) for k, v in headers.items()} if headers else None
    request_params = {str(k): str(v) for k, v in params.items()} if params else None
    request_data = {'json': data} if data is not None else {}

    limit = MAX_IMAGE_RESPONSE_BYTES if normalized_format == 'image' else MAX_TEXT_RESPONSE_BYTES
    http_transport = transport if transport is not None else _default_http_transport

    _log.info(
        f'已创建 HTTP 请求 {normalized_method} {normalized_url}，'
        f'content_format={normalized_format}，超时 {request_timeout:g}s')

    try:
        async with httpx.AsyncClient(
                timeout=request_timeout,
                follow_redirects=True,
                transport=http_transport,
        ) as client, client.stream(
                normalized_method,
                normalized_url,
                headers=request_headers,
                params=request_params,
                **request_data,
        ) as response:
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                total += len(chunk)
                if total > limit:
                    _log.warning(f'HTTP 响应内容超过限制（{total} > {limit} 字节）')
                    return _generate_tool_payload(
                        'error', f'响应内容过大（超过 {limit} 字节），已中止读取',
                        {'status_code': response.status_code, 'limit': limit})
                chunks.append(chunk)
            content = b''.join(chunks)

            _log.info(
                f'HTTP 请求 {normalized_method} {normalized_url} 返回状态码 '
                f'{response.status_code}，{total} 字节')
            message = _http_message(response)
            content_type = (response.headers.get('content-type') or '').split(';')[0].strip().lower()

            # 非 2xx：仍然返回响应片段，方便模型判断错误原因
            if response.status_code >= 400:
                text = _decode_http_text(response, content)
                return _generate_tool_payload(
                    'error', message,
                    {'status_code': response.status_code, 'content': text[:2000]})

            if normalized_format == 'image':
                if content_type not in ALLOWED_IMAGE_MIME_TYPES:
                    return _generate_tool_payload(
                        'error', f'响应不是受支持的图片类型: {content_type or "unknown"}',
                        {'status_code': response.status_code,
                         'supported': sorted(ALLOWED_IMAGE_MIME_TYPES)})
                if not content:
                    return _generate_tool_payload('error', '响应图片内容为空')
                return _generate_vision_tool_payload(
                    content, content_type, normalized_url, response.status_code)

            text = _decode_http_text(response, content)

            if normalized_format == 'json':
                try:
                    parsed = json.loads(text)
                except ValueError:
                    return _generate_tool_payload(
                        'error', '响应内容不是有效的 JSON',
                        {'status_code': response.status_code, 'content': text[:2000]})
                return _generate_tool_payload('success', message, parsed)

            if normalized_format == 'text':
                if not text.strip():
                    return _generate_tool_payload('error', '响应内容为空',
                                                  {'status_code': response.status_code})
                return _generate_tool_payload(
                    'success', message,
                    {'status_code': response.status_code, 'content': text})

            # markdown：响应本身是 Markdown 就直接返回，否则尝试把 HTML 转成 Markdown
            looks_like_html = bool(re.search(
                r'<\s*(html|head|body|div|p|a|span|table|h[1-6]|ul|ol|li)\b',
                text, re.IGNORECASE))
            looks_like_markdown = bool(re.search(r'(^|\n)(#{1,6}\s|\*\s|-\s|\d+\.\s)', text))
            if looks_like_html or not looks_like_markdown:
                try:
                    _log.debug('响应内容不是 Markdown，尝试将 HTML 转换为 Markdown')
                    text = _markdownify_html(text)
                except Exception as e:
                    _log.warning(f'将 HTML 转换为 Markdown 失败: {e}')
                    return _generate_tool_payload(
                        'error', f'响应内容不是有效的 Markdown 文本，且转换失败: {e}',
                        {'status_code': response.status_code, 'content': text[:2000]})
            return _generate_tool_payload(
                'success', message,
                {'status_code': response.status_code, 'content': text})

    except httpx.TimeoutException:
        _log.warning(f'HTTP 请求超时: {normalized_method} {normalized_url}')
        return _generate_tool_payload('error', f'请求超时（{request_timeout:g} 秒）')
    except httpx.HTTPError as e:
        _log.warning(f'HTTP 请求失败: {normalized_method} {normalized_url}: {e}')
        return _generate_tool_payload('error', f'HTTP 请求失败: {e}')
    except Exception as e:
        _log.error(f'HTTP 请求出现未预期错误: {normalized_method} {normalized_url}: {e}')
        return _generate_tool_payload('error', f'请求出现未预期错误: {e}')
