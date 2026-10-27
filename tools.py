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

import asyncio
import base64
import ipaddress
import json
import os
import re
import socket
import uuid
from datetime import datetime, timezone
from functools import partial
from typing import Any
from urllib.parse import urljoin, urlparse

import httpx
import markdownify
from ncatbot.api.qq import QQAPIClient
from ncatbot.event.qq import GroupMessageEvent, MessageEvent
from ncatbot.service.builtin.schedule.parser import TimeTaskParser
from ncatbot.utils.logger import get_log

__all__ = [
    'MIN_SCHEDULED_INTERVAL_SECONDS',
    '_generate_tool_payload',
    '_generate_vision_tool_payload',
    'access_memory',
    'create_http_request',
    'extract_vision_payload',
    'fetch_image_data_url',
    'get_environment_info',
    'get_group_info',
    'get_stranger_info',
    'get_system_time',
    'manage_cron_tasks',
    'query_missing_message_context',
    'register_scheduled_task',
    'tools',
    'validate_schedule_interval',
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
# 重定向上限：重定向不由 httpx 自动跟随，而是手动逐跳处理并重新校验目标主机，
# 防止「公网 URL 302 到内网地址」绕过 SSRF 防护
MAX_HTTP_REDIRECTS = 5
# 默认 transport（None 表示使用 httpx 默认实现；测试中可注入 MockTransport）
_default_http_transport: httpx.AsyncBaseTransport | None = None

# ---- 定时任务工具限制 ----
SCHEDULED_TASK_TOOL_NAME = 'manage_cron_tasks'
# 框架的定时任务名全局唯一（跨插件共享命名空间），加插件前缀避免冲突
SCHEDULED_TASK_PREFIX = 'OpenAIChatPlugin:'
# 任务名允许的字符与长度（同时作为持久化字典的键）
_TASK_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]{1,32}$')
# 最小调度间隔（秒）。写死的下限，防止模型/用户创建高频任务刷屏或耗尽额度；
# 一次性任务同样要求距离当前时间不少于该秒数
MIN_SCHEDULED_INTERVAL_SECONDS = 60
# 连续执行失败达到该次数后自动停用任务，避免永久性错误（被踢出群、密钥失效等）
# 按原间隔无限重试并刷日志
MAX_TASK_CONSECUTIVE_FAILURES = 5

_INTERVAL_UNIT_SECONDS = {'d': 86400, 'h': 3600, 'm': 60, 's': 1}
# 一次性任务的时间格式（框架支持两种写法）。注意：任务触发时该时间已过，
# `TimeTaskParser` 会判定为"过期"而解析失败，因此这里用格式判断而不是重新解析
_ONCE_INTERVAL_RE = re.compile(
    r'^(?:\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}|\d{4}:\d{2}:\d{2}-\d{2}:\d{2}:\d{2})$')
# 复合单位（如 2h30m）的正则：框架解析器只支持单一单位（2h / 30m），
# 这里把多段单位累加后统一换算成秒再交给框架
_INTERVAL_PART_RE = re.compile(r'(\d+(?:\.\d+)?)([dhms])', re.IGNORECASE)
_COMPOSITE_INTERVAL_RE = re.compile(r'(?:\d+(?:\.\d+)?[dhms])+', re.IGNORECASE)

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
                           'Use content_format="image" to fetch an image and view it. '
                           'Only public internet addresses are reachable; loopback, '
                           'private/local network and cloud-metadata addresses are blocked.',
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
    },
    {
        'type': 'function',
        'function': {
            'name': SCHEDULED_TASK_TOOL_NAME,
            'description': 'Manage scheduled tasks. New tasks always deliver their '
                           'generated reply back to the current chat session only. '
                           'Tasks created in any session are visible via action="list", '
                           'but only tasks created in the current session can be deleted. '
                           'Use action="add" to create, "delete" to remove, "list" to view tasks.',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['add', 'delete', 'list'],
                        'description': 'The operation to perform.'
                    },
                    'task_name': {
                        'type': 'string',
                        'description': 'Unique task name (letters/digits/underscore/hyphen, 1-32 chars). '
                                       'Required for add and delete.'
                    },
                    'interval': {
                        'type': 'string',
                        'description': "When to run, e.g. '30s', '2h30m', '0.5d', 'HH:MM' (daily) "
                                       "or 'YYYY-MM-DD HH:MM:SS' (one-shot). Required for add. "
                                       "Minimum interval is 60 seconds; a one-shot time must be at "
                                       "least 60 seconds in the future."
                    },
                    'prompt': {
                        'type': 'string',
                        'description': 'The instruction sent to the model each time the task runs. '
                                       'Required for add.'
                    }
                },
                'required': ['action']
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


async def fetch_image_data_url(
        source: str,
        transport: httpx.AsyncBaseTransport | None = None,
        allow_loopback_ips: bool = False,
        allow_local_network_ips: bool = False,
) -> dict[str, Any] | None:
    """下载图片并转为视觉输入所需的 data URL 信息。

    用于把用户消息中携带的图片（``Image`` 消息段的 url/file）拉取为
    OpenAI ``image_url`` 可用的 data URL。失败时返回 ``None``，不向上抛异常。

    http/https 来源同样受 SSRF 防护：回环/内网/云元数据等受保护地址段会被
    拒绝（分别受 ``allow_loopback_ips`` / ``allow_local_network_ips`` 控制），
    重定向手动逐跳跟随并重新校验。

    :param source: 图片地址（http/https URL 或本地文件路径）
    :param transport: 可选的自定义 httpx transport（测试用）
    :param allow_loopback_ips: 是否允许访问回环地址（对应配置 ``AllowLoopbackIps``）
    :param allow_local_network_ips: 是否允许访问内网地址（对应配置 ``AllowLocalNetworkIps``）
    :return: 含 ``data_url`` / ``mime_type`` / ``size`` 的 dict；无法识别/下载失败返回 None
    """
    if not isinstance(source, str):
        return None
    source = source.strip()
    if not source:
        return None

    # 本地文件：直接读取字节（http/https 之外的来源一律按本地路径处理）
    if not re.match(r'^https?://', source, re.IGNORECASE):
        if not os.path.isfile(source):
            _log.debug(f'图片本地文件不存在: {source}')
            return None
        try:
            with open(source, 'rb') as f:
                content = f.read(MAX_IMAGE_RESPONSE_BYTES + 1)
        except OSError as e:
            _log.warning(f'读取本地图片失败: {source}: {e}')
            return None
    else:
        http_transport = transport if transport is not None else _default_http_transport
        content: bytes | None = None
        try:
            async with httpx.AsyncClient(
                    timeout=DEFAULT_HTTP_TIMEOUT, follow_redirects=False,
                    transport=http_transport,
            ) as client:
                hop_url = source
                for hop in range(MAX_HTTP_REDIRECTS + 1):
                    # 每一跳都重新校验目标主机（与 HTTP 工具同一道 SSRF 闸门）
                    hit, hit_reason = await _check_protected_host(
                        hop_url, allow_loopback_ips, allow_local_network_ips)
                    if hit:
                        _log.warning(f'图片下载被拦截: {hop_url}（目标{hit_reason}）')
                        return None
                    async with client.stream('GET', hop_url) as response:
                        if response.is_redirect:
                            if hop >= MAX_HTTP_REDIRECTS:
                                _log.warning(f'图片重定向次数过多: {source}')
                                return None
                            next_url, redirect_error = _resolve_redirect_target(
                                response.headers.get('location') or '', hop_url)
                            if redirect_error:
                                _log.warning(
                                    f'图片重定向被拒绝: {hop_url}（{redirect_error}）')
                                return None
                            hop_url = next_url
                            continue
                        if response.status_code >= 400:
                            _log.warning(f'下载图片失败（状态码 {response.status_code}）: {source}')
                            return None
                        chunks: list[bytes] = []
                        total = 0
                        async for chunk in response.aiter_bytes():
                            total += len(chunk)
                            if total > MAX_IMAGE_RESPONSE_BYTES:
                                _log.warning(f'图片超过大小限制（> {MAX_IMAGE_RESPONSE_BYTES} 字节），已中止读取: {source}')
                                return None
                            chunks.append(chunk)
                        content = b''.join(chunks)
                        break
        except httpx.HTTPError as e:
            _log.warning(f'下载图片失败: {source}: {e}')
            return None
        except Exception as e:
            _log.error(f'下载图片出现未预期错误: {source}: {e}')
            return None
        if content is None:
            return None

    if not content:
        return None
    if len(content) > MAX_IMAGE_RESPONSE_BYTES:
        _log.warning(f'图片超过大小限制（{len(content)} > {MAX_IMAGE_RESPONSE_BYTES} 字节）: {source}')
        return None

    # 依据魔数推断 MIME 类型（不依赖扩展名 / Content-Type）
    mime_type = _sniff_image_mime(content)
    if mime_type is None:
        _log.debug(f'无法识别图片类型（魔数不匹配）: {source}')
        return None

    encoded = base64.b64encode(content).decode('ascii')
    return {
        'type': 'image',
        'mime_type': mime_type,
        'size': len(content),
        'source': source,
        'data_url': f'data:{mime_type};base64,{encoded}',
    }


def _sniff_image_mime(content: bytes) -> str | None:
    """按文件头魔数推断图片 MIME 类型；无法识别返回 None。

    :param content: 图片原始字节
    :return: MIME 类型（如 image/png）或 None
    """
    # PNG: 89 50 4E 47 0D 0A 1A 0A
    if content.startswith(b'\x89PNG\r\n\x1a\n'):
        return 'image/png'
    # JPEG: FF D8 FF
    if content.startswith(b'\xff\xd8\xff'):
        return 'image/jpeg'
    # GIF: GIF87a / GIF89a
    if content.startswith((b'GIF87a', b'GIF89a')):
        return 'image/gif'
    # WEBP: RIFF....WEBP（RIFF 于 0-3，WEBP 于 8-11）
    if len(content) >= 12 and content.startswith(b'RIFF') and content[8:12] == b'WEBP':
        return 'image/webp'
    # BMP: BM
    if content.startswith(b'BM'):
        return 'image/bmp'
    return None


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

# ======================================================================
# HTTP 请求工具
# ======================================================================

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


async def _default_dns_resolver(host: str) -> list[str]:
    """解析主机名的全部 A/AAAA 记录（去重）；解析失败返回空列表。

    通过事件循环的线程池执行 ``socket.getaddrinfo``，不阻塞事件循环。
    """
    loop = asyncio.get_running_loop()
    try:
        infos = await loop.getaddrinfo(host, None)
    except (socket.gaierror, OSError):
        return []
    return list({info[4][0] for info in infos})


# DNS 解析器：默认走 asyncio；测试中可注入 stub，避免真实 DNS 请求
_dns_resolver = _default_dns_resolver


def _classify_ip(ip_str: str) -> str | None:
    """判定 IP 地址命中的受保护类别；无需拦截时返回 None。

    拦截类别（中文原因，仅用于日志）：

    - ``回环地址``：受 ``allow_loopback_ips`` 控制（127.0.0.0/8、::1）
    - ``内网地址``：受 ``allow_local_network_ips`` 控制
      （10/8、172.16/12、192.168/16、fc00::/7 等）
    - 其余命中段**一律拦截（无配置项）**：链路本地（含云元数据
      169.254.169.254）、未指定（0.0.0.0、::）、组播、保留段、
      非全局可路由地址（如 CGNAT 100.64/10）

    :param ip_str: IP 地址字符串（IPv4 或 IPv6）
    :return: 命中类别，或 None
    """
    try:
        addr = ipaddress.ip_address(ip_str)
    except ValueError:
        return None
    # IPv4-mapped IPv6（如 ::ffff:127.0.0.1）按其内嵌的 IPv4 判定，
    # 防止用 IPv6 写法绕过校验
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped is not None:
        addr = addr.ipv4_mapped
    if addr.is_loopback:
        return '回环地址'
    if addr.is_link_local:
        return '链路本地地址（含云元数据）'
    if addr.is_unspecified or addr.is_multicast:
        return '未指定/组播地址'
    if addr.is_reserved:
        return '保留地址段'
    if addr.is_private:
        return '内网地址'
    if not addr.is_global:
        return '非全局可路由地址'
    return None


def _parse_ip_literal(host: str) -> list[str] | None:
    """主机为 IP 字面量时返回 ``[host]``；否则返回 None（交给 DNS 解析）。"""
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return None
    return [host]


async def _check_protected_host(
        url: str,
        allow_loopback_ips: bool = False,
        allow_local_network_ips: bool = False,
) -> tuple[bool, str]:
    """检查 HTTP 目标主机是否命中受保护地址段（SSRF 防护）。

    主机为 IP 字面量时直接判定；否则经 DNS 解析后对所有 A/AAAA 记录逐条判定
    （任一记录命中即拒绝，防止目标主机同时返回公网与内网记录）。DNS 解析
    失败一律拒绝（fail-closed）。

    :param url: 请求 URL
    :param allow_loopback_ips: 是否允许回环地址（对应配置 ``AllowLoopbackIps``）
    :param allow_local_network_ips: 是否允许内网地址（对应配置 ``AllowLocalNetworkIps``）
    :return: ``(是否命中受保护地址, 命中原因)``；未命中时原因为空串
    """
    host = (urlparse(url).hostname or '').lower()
    if not host:
        return False, ''
    ip_strs = _parse_ip_literal(host)
    if ip_strs is None:
        ip_strs = await _dns_resolver(host)
        if not ip_strs:
            return True, 'DNS 解析失败'
    for ip_str in ip_strs:
        category = _classify_ip(ip_str)
        if category is None:
            continue
        if category == '回环地址' and allow_loopback_ips:
            continue
        if category == '内网地址' and allow_local_network_ips:
            continue
        return True, category
    return False, ''


def _resolve_redirect_target(location: str, base_url: str) -> tuple[str | None, str | None]:
    """把重定向 Location 解析为绝对 URL 并校验，返回 (绝对 URL, 错误信息)。

    仅允许 http/https 且不含 userinfo 的目标（相对路径按当前 URL 拼接）。
    """
    stripped = (location or '').strip()
    if not stripped:
        return None, '重定向目标缺失'
    target = urljoin(base_url, stripped)
    parsed = urlparse(target)
    if parsed.scheme not in ('http', 'https'):
        return None, '重定向目标必须是 http/https'
    if not parsed.netloc or parsed.username or parsed.password:
        return None, '重定向目标非法'
    return target, None


def _same_origin(url_a: str, url_b: str) -> bool:
    """判断两个 URL 是否同源（scheme / host / port 完全相同）。"""
    parsed_a, parsed_b = urlparse(url_a), urlparse(url_b)
    return (
        parsed_a.scheme == parsed_b.scheme
        and parsed_a.hostname == parsed_b.hostname
        and parsed_a.port == parsed_b.port
    )


def _redirect_method(method: str, status_code: int) -> str:
    """按 httpx/浏览器的默认语义决定重定向后的 HTTP 方法。

    - 303：除 HEAD 外一律改为 GET
    - 302：除 HEAD 外一律改为 GET（浏览器行为）
    - 301：仅 POST 改为 GET
    - 307/308：保持原方法
    """
    if status_code == 303 and method != 'HEAD':
        return 'GET'
    if status_code == 302 and method != 'HEAD':
        return 'GET'
    if status_code == 301 and method == 'POST':
        return 'GET'
    return method


def _redirect_headers(headers: dict[str, str] | None, source_url: str,
                      target_url: str, method_changed_to_get: bool) -> dict[str, str]:
    """按 httpx 的默认语义清理跨重定向的请求头。

    手动逐跳跟随重定向后，httpx 不会再替我们做这些清理，因此必须显式处理：

    - 跨源（scheme / host / port 任一不同）时移除 ``Authorization`` /
      ``Proxy-Authorization``（http→https 同主机跳转保留，与 httpx 一致）
    - 任何重定向都移除 ``Cookie``（没有客户端 cookie jar，不携带来源不明的 cookie）
    - 跨源时移除 ``Host``，由 httpx 依据目标 URL 重新生成
    - 方法降级为 GET 时移除 ``Content-Length`` / ``Transfer-Encoding``
    """
    if not headers:
        return {}
    cleaned = dict(headers)
    lower_keys = {key.lower(): key for key in cleaned}

    def _pop(name: str) -> None:
        key = lower_keys.get(name)
        if key is not None:
            cleaned.pop(key, None)

    source = urlparse(source_url)
    target = urlparse(target_url)
    if not _same_origin(source_url, target_url):
        is_https_upgrade = source.scheme == 'http' and target.scheme == 'https'
        if not (is_https_upgrade and source.hostname == target.hostname):
            _pop('authorization')
            _pop('proxy-authorization')
        _pop('host')
    _pop('cookie')
    if method_changed_to_get:
        _pop('content-length')
        _pop('transfer-encoding')
    return cleaned

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
        allow_loopback_ips: bool = False,
        allow_local_network_ips: bool = False,
        **_extra: Any,
) -> str:
    """创建 HTTP 请求并返回适合模型阅读的 JSON payload

    - ``content_format``：json / text / markdown / image
    - ``image`` 模式下会返回 data URL，主流程会将其转换为视觉消息附加到对话
    - 文本/网页响应默认限制 2 MiB，图片限制 5 MiB
    - **SSRF 防护**：请求前后逐跳校验目标主机，回环/内网/云元数据等受保护
      地址段会被拒绝（分别受 ``allow_loopback_ips`` /
      ``allow_local_network_ips`` 控制，其余段一律拦截）；重定向手动逐跳
      跟随，每一跳重新校验
    - 所有网络异常、参数错误都会转为 ``status: error`` 的 payload，不向上抛异常

    :param method: HTTP 方法，如 'GET'、'POST'
    :param url: 请求 URL（仅 http/https）
    :param content_format: 期望的响应内容格式
    :param data: 可选的 JSON 请求体（dict/list）
    :param headers: 可选的请求头
    :param params: 可选的 URL 查询参数
    :param timeout: 超时秒数（默认 15，最大 60）
    :param transport: 可选的自定义 httpx transport（测试用）
    :param allow_loopback_ips: 是否允许访问回环地址（对应配置 ``AllowLoopbackIps``）
    :param allow_local_network_ips: 是否允许访问内网地址（对应配置 ``AllowLocalNetworkIps``）
    :param _extra: 忽略模型多传的未知参数
    :return: json 字符串
    """
    # 规范化参数
    normalized_method = _normalize_http_method(method)
    if normalized_method is None:
        return _generate_tool_payload('error', f'不支持的 HTTP 方法: {method}')

    # 规范化 URL 并校验
    normalized_url, url_error = _validate_http_url(url)
    if url_error:
        return _generate_tool_payload('error', url_error)

    # 规范化 content_format 并校验
    normalized_format = (content_format or 'text').strip().lower() if isinstance(content_format, str) else ''
    if normalized_format not in ALLOWED_CONTENT_FORMATS:
        return _generate_tool_payload(
            'error', f'不支持的 content_format: {content_format}',
            {'supported': sorted(ALLOWED_CONTENT_FORMATS)})

    # 参数类型校验
    if data is not None and not isinstance(data, (dict, list)):
        return _generate_tool_payload('error', 'data 必须是 JSON 对象或数组')
    if headers is not None and not isinstance(headers, dict):
        return _generate_tool_payload('error', 'headers 必须是对象')
    if params is not None and not isinstance(params, dict):
        return _generate_tool_payload('error', 'params 必须是对象')

    # 超时参数校验
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
                # 手动逐跳跟随重定向：每一跳都重新校验目标主机，
                # 防止「公网 URL 302 到内网地址」绕过 SSRF 防护
                follow_redirects=False,
                transport=http_transport,
        ) as client:
            # 重定向前可能发生变化的方法 / 请求头 / 查询参数 / 请求体
            hop_method = normalized_method
            hop_url = normalized_url
            hop_params = request_params
            hop_headers = request_headers
            hop_data = request_data
            hops = 0
            while True:
                # 每一跳都重新做主机校验（SSRF 防护）
                hit, hit_reason = await _check_protected_host(
                    hop_url, allow_loopback_ips, allow_local_network_ips)
                if hit:
                    _log.warning(
                        f'HTTP 请求被拦截: {hop_method} {hop_url}'
                        f'（目标{hit_reason}）')
                    return _generate_tool_payload(
                        'error', '请求失败：目标地址被禁止访问')

                async with client.stream(
                        hop_method,
                        hop_url,
                        headers=hop_headers,
                        params=hop_params,
                        **hop_data,
                ) as response:
                    if response.is_redirect:
                        if hops >= MAX_HTTP_REDIRECTS:
                            _log.warning(f'HTTP 请求重定向次数过多: {normalized_url}')
                            return _generate_tool_payload(
                                'error', '请求失败：重定向次数过多')
                        next_url, redirect_error = _resolve_redirect_target(
                            response.headers.get('location') or '', hop_url)
                        if redirect_error:
                            _log.warning(
                                f'HTTP 请求重定向被拒绝: {hop_url}（{redirect_error}）')
                            return _generate_tool_payload(
                                'error', f'请求失败：{redirect_error}')
                        # 303/302 除 HEAD 外降级为 GET、301 仅 POST 降级（与 httpx
                        # 默认行为一致）；307/308 保持原方法与请求体。
                        # 每一跳都重新清理请求头（跨源不携带 Authorization/Cookie/Host），
                        # 原查询参数不带到下一跳
                        next_method = _redirect_method(hop_method, response.status_code)
                        hop_headers = _redirect_headers(
                            hop_headers, hop_url, next_url,
                            method_changed_to_get=next_method != hop_method)
                        if next_method != hop_method:
                            hop_method = next_method
                            hop_data = {}
                        hop_params = None
                        hop_url = next_url
                        hops += 1
                        continue

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
                            content, content_type, hop_url, response.status_code)

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
        return _generate_tool_payload('error', '请求失败：目标响应超时')
    except httpx.HTTPError as e:
        _log.warning(f'HTTP 请求失败: {normalized_method} {normalized_url}: {e}')
        # 错误文案不区分「连接被拒 / 超时 / DNS 失败」等具体原因，避免被当作
        # 端口与路径存在性探测的 oracle（SSRF 防护的一部分）
        return _generate_tool_payload('error', '请求失败：无法访问目标地址')
    except Exception as e:
        _log.error(f'HTTP 请求出现未预期错误: {normalized_method} {normalized_url}: {e}')
        return _generate_tool_payload('error', '请求失败：服务器内部错误')


# ======================================================================
# 定时任务工具
# ======================================================================

def _scheduled_task_key(task_name: str) -> str:
    """把用户任务名转换为框架任务名（加插件前缀，避免跨插件重名）。"""
    return f'{SCHEDULED_TASK_PREFIX}{task_name}'


def _normalize_schedule_interval(interval: Any) -> tuple[str, dict] | None:
    """解析 interval，并返回可直接交给框架的规范化结果。

    优先复用框架的 ``TimeTaskParser``（保证语义一致）；框架只支持单一单位，
    因此 ``2h30m`` 这类复合单位由插件自行累加换算成秒（``9000s``）。

    :param interval: 调度时间参数（字符串或数字）
    :return: ``(交给框架的 interval, 解析结果)``；无法解析时返回 None
    """
    text = str(interval)
    try:
        return text, TimeTaskParser.parse(text)
    except (TypeError, ValueError):
        pass

    if _COMPOSITE_INTERVAL_RE.fullmatch(text):
        total = sum(float(num) * _INTERVAL_UNIT_SECONDS[unit.lower()]
                    for num, unit in _INTERVAL_PART_RE.findall(text))
        if total > 0:
            return f'{int(total)}s', {'type': 'interval', 'value': total}
    return None


def _resolve_schedule_interval(interval: Any) -> tuple[str | None, str | None]:
    """解析并校验调度时间参数。

    :param interval: 调度时间参数（字符串或数字）
    :return: ``(交给框架的 interval, 错误文案)``，合法时错误文案为 None
    """
    parsed = _normalize_schedule_interval(interval)
    if parsed is None:
        return None, (f'interval 格式非法或一次性时间已过期: {interval}，支持 '
                      f'30s / 2h30m / 0.5d / HH:MM / YYYY-MM-DD HH:MM:SS')
    normalized, config = parsed
    if config['type'] == 'daily':
        return normalized, None
    # 'interval' 的 value 是间隔秒数，'once' 的 value 是距离当前的秒数
    seconds = float(config['value'])
    if seconds < MIN_SCHEDULED_INTERVAL_SECONDS:
        return None, (f'interval 不能小于 {MIN_SCHEDULED_INTERVAL_SECONDS} 秒'
                      f'（当前约 {int(seconds)} 秒）')
    return normalized, None


def validate_schedule_interval(interval: Any) -> str | None:
    """校验调度时间参数是否满足最小间隔要求。

    解析失败、一次性任务时间已过期、间隔小于 ``MIN_SCHEDULED_INTERVAL_SECONDS``
    都视为非法；每日任务（``HH:MM``）天然大于下限，直接放行。

    :param interval: 调度时间参数（字符串或数字）
    :return: 错误文案；合法时返回 None
    """
    return _resolve_schedule_interval(interval)[1]


def register_scheduled_task(plugin, task_name: str, interval: str, prompt: str,
                            session_type: str, session_id: str) -> bool:
    """向框架注册一个定时任务（工具创建与启动恢复共用）。

    注册前统一做最小间隔校验，因此工具创建与重启恢复走同一道闸；
    历史记录中不合规的 interval 不会注册到调度器。

    :param plugin: 插件实例（需具备 add_scheduled_task）
    :param task_name: 任务名（不含插件前缀）
    :param interval: 调度时间参数（复合单位会被规范化后交给框架）
    :param prompt: 每次执行时发给模型的提示
    :param session_type: 'group' 或 'private'
    :param session_id: 投递目标会话 ID
    :return: 是否注册成功（框架以返回值而非异常表示失败）
    """
    normalized, error = _resolve_schedule_interval(interval)
    if error:
        _log.warning(f'定时任务 {task_name} 注册失败: {error}')
        return False
    return plugin.add_scheduled_task(
        _scheduled_task_key(task_name),
        normalized,
        callback=partial(
            _cron_task_callback, plugin, task_name, prompt, session_type, session_id),
    )


def _task_records(plugin) -> dict:
    """读取定时任务记录字典（缺失时补建，保持与其他会话数据结构一致）。"""
    return plugin.data.setdefault('data', {}).setdefault('scheduled_tasks', {})


def _get_task_record(plugin, task_name: str) -> dict | None:
    """读取单个任务记录，非 dict（损坏）时返回 None。"""
    record = _task_records(plugin).get(task_name)
    return record if isinstance(record, dict) else None


def _is_once_task(record: dict) -> bool:
    """判断记录是否为一次性任务（``YYYY-MM-DD HH:MM:SS``）。

    任务触发时目标时间已过，``TimeTaskParser`` 会判定为过期而解析失败，
    因此这里只按格式判断，避免执行后无法识别、记录清理不掉。
    """
    return bool(_ONCE_INTERVAL_RE.match(str(record.get('interval', '')).strip()))


def _drop_task_record(plugin, task_name: str) -> None:
    """删除任务记录并持久化（记录不存在时不写盘）。"""
    if _task_records(plugin).pop(task_name, None) is None:
        return
    plugin._save_data()


def _record_task_success(plugin, task_name: str) -> None:
    """执行成功：清零连续失败计数（仅有失败历史时才写盘）。"""
    record = _get_task_record(plugin, task_name)
    if not record or not record.get('failures'):
        return
    record['failures'] = 0
    plugin._save_data()


def _record_task_failure(plugin, task_name: str, reason: str) -> None:
    """执行失败：累计连续失败次数，达到上限后自动停用并移出调度器。"""
    record = _get_task_record(plugin, task_name)
    if record is None:
        return
    failures = int(record.get('failures', 0) or 0) + 1
    record['failures'] = failures
    if failures >= MAX_TASK_CONSECUTIVE_FAILURES and not record.get('disabled'):
        record['disabled'] = True
        record['disabled_reason'] = reason
        plugin.remove_scheduled_task(_scheduled_task_key(task_name))
        _log.error(
            f'定时任务 {task_name} 连续失败 {failures} 次，已自动停用: {reason}')
    plugin._save_data()


async def _cron_task_callback(plugin, task_name: str, prompt: str,
                              session_type: str, session_id: str) -> None:
    """定时任务触发：把内部消息写入主会话历史并投递模型生成的回复。

    实际的一轮对话由 ``plugin._run_scheduled_task_turn()`` 完成（共用会话历史、
    按会话加锁、不带工具、成功才落库）。这里只负责执行开关、失败计数与清理。

    任何异常都只记录日志、不向上抛（回调运行在框架 worker 线程，
    异常会被 ``TaskExecutor`` 捕获并记录，不会影响调度线程）。

    失败会累计到记录的 ``failures`` 字段，连续失败达到
    ``MAX_TASK_CONSECUTIVE_FAILURES`` 次后自动停用；一次性任务执行后
    无论成功失败都会清理记录（框架已自动移除该调度）。

    :param plugin: 插件实例
    :param task_name: 任务名（不含插件前缀）
    :param prompt: 任务提示
    :param session_type: 'group' 或 'private'
    :param session_id: 投递目标会话 ID
    """
    record = _get_task_record(plugin, task_name)
    try:
        if not plugin.get_config('EnableScheduledTaskExecution', True):
            _log.debug(f'定时任务执行总开关已关闭，跳过: name={task_name}')
            return
        _log.info(f'定时任务触发: name={task_name}, target={session_type}:{session_id}')
        runner = getattr(plugin, '_run_scheduled_task_turn', None)
        if not callable(runner):
            raise RuntimeError('插件不支持定时任务轮次（缺少 _run_scheduled_task_turn）')
        creator_id = str(record.get('creator_id', '')) if record else ''
        await runner(session_type, session_id, task_name, prompt, creator_id)
        _record_task_success(plugin, task_name)
    except Exception as e:
        _record_task_failure(plugin, task_name, str(e))
        _log.error(f'定时任务 {task_name} 执行失败: {e}')
    finally:
        # 一次性任务执行后框架会自动移除调度，这里同步清理记录避免残留
        if record is not None and _is_once_task(record):
            _drop_task_record(plugin, task_name)
            _log.info(f'一次性定时任务 {task_name} 已执行，记录已清理')


def _task_status_payload(plugin, task_name: str) -> dict:
    """读取框架侧任务状态（下次执行时间 / 已执行次数）。

    :param plugin: 插件实例
    :param task_name: 任务名（不含插件前缀）
    :return: 状态字典（任务不在调度器中时返回空字典）
    """
    try:
        status = plugin.get_task_status(_scheduled_task_key(task_name))
    except Exception as e:
        _log.debug(f'读取定时任务 {task_name} 状态失败: {e}')
        return {}
    if not status:
        return {}
    next_run = status.get('next_run')
    if next_run is not None:
        next_run = next_run.isoformat() if hasattr(next_run, 'isoformat') else str(next_run)
    return {'next_run': next_run, 'run_count': status.get('run_count')}


async def manage_cron_tasks(plugin, action: str, task_name: str = '', interval: str = '',
                            prompt: str = '', session_type: str = 'group',
                            session_id: str = '', creator_id: str = '',
                            **_extra: Any) -> str:
    """管理定时任务：增（add）/删（delete）/查（list）

    投递目标由插件按当前会话注入（模型无法指定其他群或用户），任务记录保存在
    ``data['data']['scheduled_tasks']``，重启后由 ``on_load`` 恢复注册。

    任务**不做会话隔离**：``list`` 能看到所有会话的任务记录，但 ``add`` 只能创建到
    当前会话、``delete`` 只能删除当前会话创建的任务（跨会话删除会被拒绝）。

    :param plugin: 插件实例
    :param action: 操作类型，支持 add / delete / list
    :param task_name: 任务名（字母/数字/下划线/连字符，1~32 字符）
    :param interval: 调度时间参数，支持 ``30s`` / ``2h30m`` / ``0.5d`` / ``HH:MM`` / ``YYYY-MM-DD HH:MM:SS``，
        最小间隔 ``MIN_SCHEDULED_INTERVAL_SECONDS`` 秒
    :param prompt: 每次执行时发送给模型的提示（仅 add）
    :param session_type: 投递类型 'group' / 'private'（由插件注入）
    :param session_id: 投递目标会话 ID（由插件注入）
    :param creator_id: 创建者用户 ID（由插件注入，供定时轮的记忆/环境工具使用）
    :param _extra: 忽略模型多传的未知参数
    :return: json 字符串，包含操作结果
    """
    tasks = plugin.data['data'].setdefault('scheduled_tasks', {})

    if action == 'add':
        if not task_name or not _TASK_NAME_RE.match(task_name):
            return _generate_tool_payload(
                'error', 'task_name 只能包含字母/数字/下划线/连字符，长度 1~32')
        if not interval:
            return _generate_tool_payload('error', '添加任务时必须提供 interval')
        interval_error = validate_schedule_interval(interval)
        if interval_error:
            return _generate_tool_payload('error', interval_error)
        if not plugin.get_config('EnableScheduledTaskExecution', True):
            return _generate_tool_payload(
                'error', '定时任务执行已暂停，无法创建新任务')
        if not prompt or not prompt.strip():
            return _generate_tool_payload('error', '添加任务时必须提供 prompt')
        if not session_id:
            return _generate_tool_payload('error', '无法确定投递会话，已取消创建')
        if task_name in tasks:
            return _generate_tool_payload(
                'error', f'任务 {task_name} 已存在，请先删除或换一个名称')

        if not register_scheduled_task(
                plugin, task_name, interval, prompt, session_type, str(session_id)):
            return _generate_tool_payload(
                'error', f'任务 {task_name} 添加失败（interval 非法或定时任务服务不可用）')

        tasks[task_name] = {
            'interval': interval,
            'prompt': prompt,
            'session_type': session_type,
            'session_id': str(session_id),
            'creator_id': str(creator_id or ''),
            'created_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'),
        }
        plugin._save_data()
        _log.info(
            f'已创建定时任务: name={task_name}, interval={interval}, '
            f'target={session_type}:{session_id}')
        return _generate_tool_payload('success', f'任务 {task_name} 添加成功', tasks[task_name])

    if action == 'delete':
        if not task_name:
            return _generate_tool_payload('error', '删除任务时必须提供 task_name')
        if task_name not in tasks:
            return _generate_tool_payload('error', f'任务 {task_name} 不存在')
        owner = tasks[task_name]
        if not isinstance(owner, dict):
            return _generate_tool_payload('error', f'任务 {task_name} 的记录已损坏，无法删除')
        # 跨会话删除一律拒绝：任务可以查看，但只能由创建它的会话管理
        if (str(owner.get('session_id', '')) != str(session_id)
                or owner.get('session_type', 'group') != session_type):
            _log.warning(
                f'拒绝跨会话删除定时任务: name={task_name}, '
                f'owner={owner.get("session_type")}:{owner.get("session_id")}, '
                f'requester={session_type}:{session_id}')
            return _generate_tool_payload(
                'error', f'任务 {task_name} 不属于当前会话，无法删除')
        removed = plugin.remove_scheduled_task(_scheduled_task_key(task_name))
        tasks.pop(task_name, None)
        plugin._save_data()
        if not removed:
            _log.warning(f'任务 {task_name} 不在调度器中，已仅清理持久化记录')
            return _generate_tool_payload(
                'success', f'任务 {task_name} 已从记录中移除（调度器中原本不存在）')
        _log.info(f'已删除定时任务: name={task_name}')
        return _generate_tool_payload('success', f'任务 {task_name} 删除成功')

    if action == 'list':
        result = []
        for name, info in tasks.items():
            item = {'name': name, **info}
            item.update(_task_status_payload(plugin, name))
            result.append(item)
        _log.info(f'已获取定时任务列表: {len(result)} 个任务')
        return _generate_tool_payload('success', f'共 {len(result)} 个任务', result)

    return _generate_tool_payload('error', f'不支持的操作类型: {action}')

