# -*- coding: utf-8 -*-
"""
数据迁移/更新实现模块

1) 记忆格式迁移（旧 memory.json -> v0.1.4+ memory.json）
2) 4.x -> 5.x 运行时数据一次性迁移（旧 data/openai_chat_plugin/ -> 插件 workspace）

预设数据结构：
<workspace>/
| -- presents/
    | -- <present_name>/  # 每个预设一个目录，目录名即预设名
        | -- config.yaml  # 本预设的配置文件
        | -- prompt.md  # 本预设使用的提示词
        | -- memory.json  # 自动创建（如果启用记忆功能）
"""

import json
import os
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ncatbot.utils.logger import get_log

_log = get_log('openai_chat_plugin.update')

# v5 DataMixin 持久化的四个会话数据键
_DATA_KEYS = ('group_conversations', 'user_conversations', 'group_preset_names', 'user_preset_names')


def is_need_update(plugin: Any) -> bool:
    """判断是否需要执行记忆格式迁移。

    判断依据（v0.1.0 -> v0.1.4 的迁移）：
    - 预设目录下存在 legacy memory.json；
    - 且 memory.json 中存在 legacy 数据。

    :param plugin: 插件实例
    :return: bool
    """
    return _should_update_memory_format(plugin)


def update_data(plugin: Any) -> None:
    """执行记忆格式迁移：将各预设目录下的 memory.json 升级到 v0.1.4+ 结构。

    注：4.x 时代的"全局配置 presents 迁移"依赖的 config.plugins_config API 已在
    NcatBot 5 中移除，该逻辑随之删除；对应场景由 migrate_legacy_workspace 覆盖。

    :param plugin: 插件实例
    :return: None
    """
    memory_migrated_any = _migrate_memory_files(plugin)
    if memory_migrated_any:
        _log.info('已完成 openai_chat_plugin 记忆格式升级')


def _legacy_workspace_dir() -> Path:
    """返回 4.x 版本的插件数据目录（相对运行目录）

    4.x 时代 register_config / self.data 均持久化到 data/openai_chat_plugin/ 下。

    :return: 旧版数据目录 Path
    """
    return Path('data') / 'openai_chat_plugin'


def migrate_legacy_workspace(plugin: Any) -> bool:
    """一次性将 4.x 运行时数据迁移到 v5 新位置，成功后旧目录改名备份。

    - 旧 presents/ 目录整目录复制到新 workspace/presents/（目标已存在则跳过）；
    - 旧 openai_chat_plugin.json 中 `data` 键下的会话数据迁入新 data.json
      （新数据已有 `data` 键则跳过，避免覆盖）；
    - 迁移完成后旧目录改名为 openai_chat_plugin.migrated.bak 防止重复执行。

    幂等性：任一步骤的目标已存在时自动跳过，重复调用无副作用。

    :param plugin: 插件实例（需具有 workspace / data / _save_data）
    :return: 是否发生了实际迁移
    """
    legacy_dir = _legacy_workspace_dir()
    if not legacy_dir.is_dir():
        return False

    migrated_any = False

    # 1) presents 目录复制
    legacy_presents = legacy_dir / 'presents'
    new_presents = Path(plugin.workspace) / 'presents'
    if legacy_presents.is_dir() and not new_presents.exists():
        shutil.copytree(legacy_presents, new_presents)
        migrated_any = True
        _log.info(f'预设目录已迁移至新工作区: {new_presents}')
    elif legacy_presents.is_dir():
        _log.debug('新工作区已存在 presents 目录，跳过旧预设复制')

    # 2) 会话数据迁移（只认 `data` 键；`config` 键属于旧版配置体系，不再迁移）
    legacy_data_file = legacy_dir / 'openai_chat_plugin.json'
    if legacy_data_file.is_file() and 'data' not in plugin.data:
        try:
            with open(legacy_data_file, 'r', encoding='utf-8') as f:
                legacy_content = json.load(f)
            legacy_data = legacy_content.get('data') if isinstance(legacy_content, dict) else None
            if isinstance(legacy_data, dict):
                conversations = {
                    key: legacy_data[key] for key in _DATA_KEYS if isinstance(legacy_data.get(key), dict)
                }
                if conversations:
                    plugin.data['data'] = {key: {} for key in _DATA_KEYS}
                    plugin.data['data'].update(conversations)
                    plugin._save_data()
                    migrated_any = True
                    _log.info(
                        f'会话数据已迁移至新 data.json'
                        f'（群会话 {len(conversations.get("group_conversations", {}))} 个，'
                        f'用户会话 {len(conversations.get("user_conversations", {}))} 个）')
                else:
                    _log.debug('旧数据文件中未发现有效的会话数据，跳过会话迁移')
        except Exception as e:
            _log.error(f'读取旧版数据文件失败，跳过会话迁移: {e}')
    elif legacy_data_file.is_file():
        _log.debug('新 data.json 已存在会话数据，跳过旧会话迁移')

    # 3) 旧目录改名备份，防止重复迁移
    if migrated_any:
        backup_dir = legacy_dir.with_name('openai_chat_plugin.migrated.bak')
        if backup_dir.exists():
            # 备份目录已存在时追加时间戳，避免覆盖
            backup_dir = legacy_dir.with_name(f'openai_chat_plugin.migrated.bak.{int(time.time())}')
        try:
            legacy_dir.rename(backup_dir)
            _log.info(f'旧版数据目录已重命名为 {backup_dir.name}（保留备份，可手动删除）')
        except Exception as e:
            _log.warning(f'旧版数据目录改名失败（不影响迁移结果，下次启动将跳过已迁移项）: {e}')

    return migrated_any


# ----------------------------------------------------------------------
# 记忆格式迁移（v0.1.0 -> v0.1.4+）
# ----------------------------------------------------------------------

def _memory_entry_is_legacy(item: Any) -> bool:
    """检查单项记忆是否为旧版记忆格式

    旧版 memory.json 仅包含：
    - id: int
    - content: str

    新版 memory.json 结构要求：参见tools.py

    :param item: 单条记忆数据
    """
    if not isinstance(item, dict):
        return False
    if 'content' not in item or 'id' not in item:
        return False
    # 只要缺少新字段，就认为是 legacy
    required_new_fields = {'from_user', 'from_group', 'create_time'}
    if not required_new_fields.issubset(set(item.keys())):
        return True

    # 旧版 id 是递增整数；新结构要求 id 为字符串（UUID）。
    if not isinstance(item.get('id'), str):
        return True

    # content 也应为字符串
    return not isinstance(item.get('content'), str)


def _should_update_memory_format(plugin: Any) -> bool:
    """检测 presents 目录下是否存在 legacy memory.json。

    :param plugin: 插件实例
    :return: bool
    """
    presents_dir = os.path.join(str(plugin.workspace), 'presents')
    if not os.path.isdir(presents_dir):
        return False

    try:
        for name in os.listdir(presents_dir):
            present_dir = os.path.join(presents_dir, name)
            if not os.path.isdir(present_dir):
                continue
            memory_file = os.path.join(present_dir, 'memory.json')
            if not os.path.exists(memory_file):
                _log.debug(f'{name}/memory.json 不存在，跳过该文件的记忆格式检查')
                continue

            memory_data = None
            try:
                with open(memory_file, 'r', encoding='utf-8') as f:
                    memory_data = json.load(f)
            except PermissionError:
                _log.error(f'权限不足，无法读取文件：{memory_file}，跳过该文件的记忆格式检查')
            except json.decoder.JSONDecodeError:
                _log.error(f'{name}/memory.json 不是有效的 JSON 文件，无法进行记忆格式检查')

            if isinstance(memory_data, list) and any(_memory_entry_is_legacy(x) for x in memory_data):
                return True
    except Exception:
        return False

    return False


def _migrate_memory_file(preset_memory_file: str) -> bool:
    """将 legacy memory.json 迁移到 v0.1.4+ 格式。

    :param preset_memory_file: memory.json 文件路径
    :return: bool
    """
    if not os.path.exists(preset_memory_file):
        return False

    with open(preset_memory_file, 'r', encoding='utf-8') as f:
        memory_data = json.load(f)

    if not isinstance(memory_data, list) or not memory_data:
        return False

    if not any(_memory_entry_is_legacy(x) for x in memory_data):
        return False

    migrated: list[dict[str, Any]] = []
    for item in memory_data:
        if not isinstance(item, dict):
            continue
        content = item.get('content', '')
        if not isinstance(content, str):
            continue

        # legacy 数据通常不包含 create_time：按约定置为 UNKNOWN。
        create_time_value = item.get('create_time', 'UNKNOWN')
        if not isinstance(create_time_value, str) or not create_time_value.strip():
            create_time_value = 'UNKNOWN'

        migrated.append({
            'id': str(uuid.uuid4()),
            'from_user': 0,  # 旧数据无法知道来源：置为全局/未知
            'from_group': -1,  # 旧数据无法知道来源：私聊/全局标记
            'create_time': create_time_value,
            'content': content
        })

    # 备份旧文件，避免误迁移不可逆
    ts = int(time.time())
    backup_path = f'{preset_memory_file}.bak.{ts}'
    shutil.copy2(preset_memory_file, backup_path)

    with open(preset_memory_file, 'w', encoding='utf-8') as f:
        json.dump(migrated, f, ensure_ascii=False, indent=2)

    return True


def _migrate_memory_files(plugin: Any) -> bool:
    """扫描 presents/<present_name>/memory.json 并进行 legacy -> v0.1.4+ 迁移。

    :param plugin: 插件实例
    :return: bool
    """
    presents_dir = os.path.join(str(plugin.workspace), 'presents')
    if not os.path.isdir(presents_dir):
        return False

    migrated_any = False
    for name in os.listdir(presents_dir):
        present_dir = os.path.join(presents_dir, name)
        if not os.path.isdir(present_dir):
            continue

        memory_file = os.path.join(present_dir, 'memory.json')
        if not os.path.exists(memory_file):
            continue

        try:
            if _migrate_memory_file(memory_file):
                migrated_any = True
                _log.info(f'记忆格式已迁移：{name}/memory.json')
        except Exception as exc:
            _log.error(f'迁移 `{name}/memory.json` 失败：{exc}')

    return migrated_any
