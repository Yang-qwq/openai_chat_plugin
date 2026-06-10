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

import json
import os
import re
from datetime import datetime, timezone
import uuid
from typing import Any

from ncatbot.core import BotAPI, BaseMessage, GroupMessage, PrivateMessage
from ncatbot.utils.logger import get_log

__all__ = ['tools', '_generate_tool_payload', 'access_memory', 'get_environment_info', 'get_stranger_info', 'get_system_time']

_log = get_log('openai_chat_plugin.tools')

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
            'name': 'get_system_time',
            'description': 'Get system time in multiple formats',
            'parameters': {
                'type': 'object',
                'properties': {},
                'required': []
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


def access_memory(
        work_space: os.PathLike | str,
        action: str,
        content: str | int | None = None,
        from_user: int | None = None,
        from_group: int | None = None,
        **_extra: object,
) -> str:
    """记忆读取、写入工具

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

    # 删除记忆：根据 id 删除记忆
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


def get_environment_info(event: GroupMessage | PrivateMessage | BaseMessage) -> str:
    """获取当前聊天环境信息

    :param event: GroupMessage | PrivateMessage | BaseMessage 消息
    :return:
    """
    # 假定group_id不存在
    group_id = None

    # 如果是群消息，则提取group_id
    if isinstance(event, GroupMessage):
        group_id = event.group_id

    environment = {
        'user_id': event.user_id,
        'group_id': group_id,
        'message_id': event.message_id,
        # 'message_type': event.message_type
    }

    return _generate_tool_payload('success', '', environment)

async def get_stranger_info(api: BotAPI, user_id: int) -> str:
    """获取用户信息

    :param api: BotAPI
    :param user_id: 用户ID
    :return: str, json字符串，包含用户信息
    """
    data = await api.get_stranger_info(user_id)

    return _generate_tool_payload('success', '', data)


async def get_group_info(api: BotAPI, group_id: int) -> str:
    """获取群聊信息

    :param api: BotAPI
    :param group_id: 群ID
    :return: str, json字符串，包含群聊信息
    """
    data = await api.get_group_info(group_id)

    return _generate_tool_payload('success', '', data)


def get_system_time() -> str:
    """获取多种格式的系统时间

    :return: str, json字符串，包含多种格式的系统时间
    """
    now = datetime.now(timezone.utc)

    return _generate_tool_payload('success', '', {
        'timestamp': now.timestamp(),
        'iso8601': now.isoformat().replace('+00:00', 'Z'),
        'rfc2822': now.strftime('%a, %d %b %Y %H:%M:%S GMT')
    })
