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

from ncatbot.utils.logger import get_log

__all__ = ['tools', '_generate_error_message', 'memory_access_tool', 'get_system_time_tool']

_log = get_log('openai_chat_plugin.tools')

tools = [
    {
        'type': 'function',
        'function': {
            'name': 'memory_access_tool',
            'description': 'Used for reading, writing and deleting memory data',
            'parameters': {
                'type': 'object',
                'properties': {
                    'action': {
                        'type': 'string',
                        'enum': ['add', 'query', 'delete'],
                        'description': "The type of operation: 'add' add memory, 'query' query memory, 'delete' delete memory by id"
                    },
                    'content': {
                        'type': 'string',
                        'description': "When action is 'add', content is the memory content to be added; when action is 'query', content is the regex for querying (optional; blank returns all memories, may cause performance issues)"
                    },
                    '_id': {
                        'type': 'string',
                        'description': "When action is 'delete', _id is the single memory id to delete"
                    }
                },
                'required': ['action']
            }
        }
    },
    {
        'type': 'function',
        'function': {
            'name': 'get_system_time_tool',
            'description': 'Get system time in multiple formats',
            'parameters': {}
        }
    }
]


def _generate_error_message(message: str, data: Any = None) -> str:
    """生成错误消息的工具函数

    :param message: 错误消息文本
    :param data:
    :return: str, json字符串，包含错误状态、消息文本和可选的附加数据
    """

    if data is None:
        return json.dumps({'status': 'error', 'message': message}, ensure_ascii=False)
    else:
        return json.dumps({'status': 'error', 'message': message, 'data': data}, ensure_ascii=False)


def memory_access_tool(
        work_space: os.PathLike | str,
        action: str,
        content: str | None = None,
        _id: str | None = None,
        from_user: int | None = None,
        from_group: int | None = None,
        **_extra: object,
) -> str:
    """记忆读取、写入工具

    :param work_space: 工作空间对象或路径
    :param action: 操作类型，add表示添加记忆，query表示查询记忆
    :param content: add 时为记忆正文；query 时为作用于每条 memory.content 的正则（省略或空白则返回全部）；delete 时不用
    :param _id: 当action为delete时，_id为要删除的记忆id（一次只能删除一条）
    :param from_user: 插件注入，用户ID
    :param from_group: 插件注入，群ID
    :return: str, json字符串，包含操作结果
    """
    # 检查操作类型是否合法
    if action not in ['add', 'query', 'delete']:
        return _generate_error_message('无效的操作类型，请使用 "add"、"query" 或 "delete"')

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
            return _generate_error_message('请提供非空的 content 字符串用于添加记忆')

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
        return json.dumps(
            {'status': 'success', 'message': '记忆添加成功'},
            ensure_ascii=False
        )

    # 查询记忆：整库 dict 项；有 content 则仅保留 memory.content 被该正则匹配的项，否则返回全部
    elif action == 'query':
        all_items = [item for item in memory_data if isinstance(item, dict)]

        if isinstance(content, str) and content.strip():
            try:
                pattern = re.compile(content.strip(), re.IGNORECASE)
            except re.error as exc:
                return _generate_error_message(f'无效的正则表达式: {exc}')

            filtered_memory = [
                item for item in all_items
                if isinstance(item.get('content'), str) and pattern.search(item['content'])
            ]
        else:
            filtered_memory = all_items

        return json.dumps({'status': 'success', 'message': '', 'data': filtered_memory}, ensure_ascii=False)

    # 删除记忆：根据 id 删除记忆
    elif action == 'delete':
        if not isinstance(_id, str):
            return _generate_error_message('请提供要删除的记忆 ID')

        visible_ids = {item.get('id') for item in memory_data if isinstance(item, dict)}
        target_id = _id

        new_memory_data = [
            item for item in memory_data
            if not (isinstance(item, dict) and item.get('id') == target_id and item.get('id') in visible_ids)
        ]

        if len(new_memory_data) == len(memory_data):
            return _generate_error_message('未找到要删除的记忆')

        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump(new_memory_data, f, ensure_ascii=False, indent=2)

        return json.dumps(
            {'status': 'success', 'message': '记忆删除成功'},
            ensure_ascii=False
        )

    return _generate_error_message('无效的操作类型')


def get_system_time_tool() -> str:
    """获取多种格式的系统时间

    :return: str, json字符串，包含多种格式的系统时间
    """
    now = datetime.now(timezone.utc)

    return json.dumps({
        'status': 'success',
        'message': '',
        'data': {
            'timestamp': now.timestamp(),
            'iso8601': now.isoformat().replace('+00:00', 'Z'),
            'rfc2822': now.strftime('%a, %d %b %Y %H:%M:%S GMT')
        }
    })
