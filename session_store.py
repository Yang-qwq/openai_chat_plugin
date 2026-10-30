# -*- coding: utf-8 -*-
"""会话消息历史的分离文件存储。

v0.5.0 起取代旧版 data.json 集中存储：每个会话独立一个文件，布局为::

    <workspace>/
    | -- group/
    |   | -- <group_id>.json    # 群会话消息数组 [{role, content}, ...]
    | -- user/
        | -- <user_id>.json     # 私聊会话消息数组

文件内容为纯消息数组（JSON list），会话维度的小状态（预设名等）仍留在
data.json，由 DataMixin 统一持久化。

内存缓存保留 ``list`` 引用：业务代码拿到消息列表后原地 ``append`` /
``history[:] = working`` 即可反映到存储层，随后调用 ``save()`` 落盘——
这一语义与旧 data.json 时代保持一致，main.py / command_handler.py 无需
改变「先改列表、再显式保存」的编写方式。

写入采用「临时文件 + os.replace」原子替换，避免进程中途崩溃产生半截
JSON。会话级并发由插件的 ``_session_locks`` 保证，本类不再加锁。
"""

import json
import os
import re
from pathlib import Path
from typing import Any

from ncatbot.utils.logger import get_log

_log = get_log('openai_chat_plugin.session_store')

# 会话目录名 <-> 旧 data.json 字典名（兼容历史调用写法）
_DIR_BY_CONVERSATION_DICT = {
    'group_conversations': 'group',
    'user_conversations': 'user',
}
_CONVERSATION_DICT_BY_DIR = {v: k for k, v in _DIR_BY_CONVERSATION_DICT.items()}

# 会话 ID 只允许出现在文件名中的安全字符（QQ 号均为纯数字）
_SAFE_SESSION_ID_PATTERN = re.compile(r'[^A-Za-z0-9_\-]')


class SessionStore:
    """按会话分离的消息历史存储（<workspace>/group|user/<id>.json）。

    :param workspace: 插件工作区路径
    """

    def __init__(self, workspace: Any):
        self._workspace = Path(workspace)
        # 内存缓存：(目录名, str(session_id)) -> 消息列表（引用，原地修改有效）
        self._cache: dict[tuple[str, str], list] = {}

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------

    @staticmethod
    def _normalize(conv_type: str) -> str:
        """归一化会话类型为目录名（兼容 'group_conversations' 旧写法）。

        :param conv_type: 'group' / 'user' 或 'group_conversations' / 'user_conversations'
        :return: 目录名 'group' 或 'user'
        :raises ValueError: 未知会话类型
        """
        if conv_type in _DIR_BY_CONVERSATION_DICT:
            return _DIR_BY_CONVERSATION_DICT[conv_type]
        if conv_type in _CONVERSATION_DICT_BY_DIR:
            return conv_type
        raise ValueError(f'未知会话类型: {conv_type}')

    @staticmethod
    def _safe_session_id(session_id: Any) -> str:
        """把会话 ID 转为可用作文件名的安全字符串。"""
        sid = str(session_id)
        return _SAFE_SESSION_ID_PATTERN.sub('_', sid) or 'unknown'

    def _path(self, conv_type: str, session_id: Any) -> Path:
        """返回会话文件路径（session_id 统一转 str）。"""
        return (self._workspace / self._normalize(conv_type)
                / f'{self._safe_session_id(session_id)}.json')

    @staticmethod
    def _write(path: Path, messages: list) -> None:
        """原子写入消息数组（临时文件 + os.replace）。"""
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(path.name + '.tmp')
        with open(tmp_path, 'w', encoding='utf-8') as f:
            json.dump(messages, f, ensure_ascii=False, indent=2)
        os.replace(tmp_path, path)

    def _load_from_disk(self, path: Path) -> list | None:
        """读取并校验会话文件；不存在或损坏时返回 None（损坏记错误日志）。"""
        try:
            with open(path, encoding='utf-8') as f:
                messages = json.load(f)
        except FileNotFoundError:
            return None
        except Exception as e:
            _log.error(f'读取会话文件失败（将视为无历史，重新初始化）: {path} -> {e}')
            return None
        if not isinstance(messages, list):
            _log.error(f'会话文件内容不是消息数组（将视为无历史）: {path}')
            return None
        return messages

    def _cache_key(self, conv_type: str, session_id: Any) -> tuple[str, str]:
        return self._normalize(conv_type), str(session_id)

    # ------------------------------------------------------------------
    # 对外 API
    # ------------------------------------------------------------------

    def load(self, conv_type: str, session_id: Any) -> list | None:
        """读取会话消息列表（带内存缓存）。

        返回缓存中的 ``list`` 引用，原地修改后调用 :meth:`save` 落盘；
        会话不存在或文件损坏时返回 ``None``。

        :param conv_type: 会话类型（目录名或旧字典名均可）
        :param session_id: 会话 ID（群号 / 用户 QQ）
        :return: 消息列表；不存在时为 None
        """
        key = self._cache_key(conv_type, session_id)
        cached = self._cache.get(key)
        if cached is not None:
            return cached
        messages = self._load_from_disk(self._path(conv_type, session_id))
        if messages is None:
            return None
        self._cache[key] = messages
        return messages

    def replace(self, conv_type: str, session_id: Any, messages: list) -> list:
        """整体替换会话消息（立即写盘，供 reset / set-preset 等场景使用）。

        :param conv_type: 会话类型
        :param session_id: 会话 ID
        :param messages: 新的消息列表（内部浅拷贝，不持有调用方列表）
        :return: 存入的列表引用（可原地修改，配合 save 使用）
        """
        key = self._cache_key(conv_type, session_id)
        stored = list(messages)
        self._cache[key] = stored
        self._write(self._path(conv_type, session_id), stored)
        return stored

    def save(self, conv_type: str, session_id: Any) -> None:
        """把缓存中的消息列表落盘（未加载的会话忽略）。

        :param conv_type: 会话类型
        :param session_id: 会话 ID
        """
        key = self._cache_key(conv_type, session_id)
        messages = self._cache.get(key)
        if messages is None:
            _log.debug(f'会话未加载，跳过保存: {key}')
            return
        self._write(self._path(conv_type, session_id), messages)

    def mtime(self, conv_type: str, session_id: Any) -> float | None:
        """返回会话文件的最后修改时间；文件不存在时为 None。

        可用作「会话最后活跃时间」的代理：分离文件只在会话发生变动
        （对话轮次落库、reset / set-preset、压缩本身）时写入，读操作不更新。
        """
        try:
            return self._path(conv_type, session_id).stat().st_mtime
        except OSError:
            return None

    def has(self, conv_type: str, session_id: Any) -> bool:
        """会话是否存在（内存缓存或磁盘文件）。"""
        if self._cache.get(self._cache_key(conv_type, session_id)) is not None:
            return True
        return self._path(conv_type, session_id).is_file()

    def ids(self, conv_type: str) -> list[str]:
        """枚举该类型的全部会话 ID（磁盘扫描 + 缓存合并，排序保证稳定顺序）。

        :param conv_type: 会话类型
        :return: 会话 ID 字符串列表
        """
        dir_name = self._normalize(conv_type)
        found: set[str] = set()
        session_dir = self._workspace / dir_name
        if session_dir.is_dir():
            for path in session_dir.glob('*.json'):
                found.add(path.stem)
        for (cached_dir, session_id) in self._cache:
            if cached_dir == dir_name:
                found.add(session_id)
        return sorted(found)

