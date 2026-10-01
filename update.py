# -*- coding: utf-8 -*-
"""
数据迁移 / 更新模块

本模块集中处理三类迁移，统一遵循「幂等、失败不阻塞插件加载」的原则：

1. 目录拼写修正（v0.2.x 内部）
   早期 v5 版本把预设目录拼写为 presents/，现统一修正为 presets/
   —— migrate_presents_dir()

2. 4.x -> 5.x 运行时数据迁移
   旧 data/openai_chat_plugin/ 中的预设目录与会话数据迁移到插件工作区
   —— migrate_legacy_workspace()

3. 记忆格式升级（v0.1.0 -> v0.1.4+）
   各预设目录下 memory.json 的旧结构升级为新结构
   —— needs_memory_migration() / migrate_memory_format()

调用顺序由 main.on_load() 决定：先修正目录拼写，再迁移 4.x 数据，最后升级记忆格式。

预设数据结构：
<workspace>/
| -- presets/
    | -- <preset_name>/   # 每个预设一个目录，目录名即预设名
        | -- config.yaml  # 本预设的配置文件
        | -- prompt.md    # 本预设使用的提示词
        | -- memory.json  # 启用记忆功能时自动创建
"""

import json
import shutil
import time
import uuid
from pathlib import Path
from typing import Any

from ncatbot.utils.logger import get_log

_log = get_log('openai_chat_plugin.update')

# v5 DataMixin 持久化的四个会话数据键
_DATA_KEYS = ('group_conversations', 'user_conversations', 'group_preset_names', 'user_preset_names')


# ======================================================================
# 一、预设目录拼写修正：presents/ -> presets/
# ======================================================================

def migrate_presents_dir(plugin: Any) -> bool:
    """将旧工作区中拼写错误的 presents/ 目录重命名为 presets/。

    仅当旧目录存在、且新目录尚不存在时执行重命名；两者并存时保留新目录并记录警告。
    失败只记录日志、不抛异常，避免阻塞插件加载。

    :param plugin: 插件实例（需具有 workspace）
    :return: 是否发生了实际迁移
    """
    legacy_dir = Path(plugin.workspace) / 'presents'
    new_dir = Path(plugin.workspace) / 'presets'
    if not legacy_dir.is_dir():
        return False
    if new_dir.exists():
        _log.warning(f'预设目录 {new_dir} 已存在，保留该目录并跳过 {legacy_dir} 的重命名')
        return False
    try:
        legacy_dir.rename(new_dir)
        _log.info(f'预设目录已由 presents/ 重命名为 presets/: {new_dir}')
        return True
    except Exception as e:
        _log.error(f'预设目录重命名失败（将继续使用旧目录，请手动处理）: {e}')
        return False


# ======================================================================
# 二、4.x -> 5.x 运行时数据迁移
# ======================================================================

def migrate_legacy_workspace(plugin: Any) -> bool:
    """一次性将 4.x 运行时数据迁移到 v5 工作区。

    旧数据目录为 cwd 下的 data/openai_chat_plugin/，主要迁移两项内容：
    - presents/：旧预设目录，整体复制为工作区的 presets/；
    - openai_chat_plugin.json 的 `data` 键：会话数据导入工作区 data.json。

    任一步骤的目标已存在时自动跳过，因此可安全重复调用（幂等）；仅当确有内容被迁移时，
    才把旧目录改名备份，防止下次启动重复迁移。

    :param plugin: 插件实例（需具有 workspace / data / _save_data）
    :return: 是否发生了实际迁移
    """
    legacy_dir = _legacy_workspace_dir()
    if not legacy_dir.is_dir():
        return False

    migrated = _copy_legacy_presets(plugin, legacy_dir)
    migrated = _import_legacy_conversations(plugin, legacy_dir) or migrated

    if migrated:
        _backup_legacy_workspace(legacy_dir)
    return migrated


def _legacy_workspace_dir() -> Path:
    """返回 4.x 版本的插件数据目录（相对运行目录）。

    4.x 时代 register_config / self.data 均持久化到 data/openai_chat_plugin/ 下。

    :return: 旧版数据目录 Path
    """
    return Path('data') / 'openai_chat_plugin'


def _copy_legacy_presets(plugin: Any, legacy_dir: Path) -> bool:
    """将旧 presents/ 目录整体复制为工作区的 presets/（目标已存在则跳过）。

    :return: 是否发生了复制
    """
    legacy_presets = legacy_dir / 'presents'
    new_presets = Path(plugin.workspace) / 'presets'
    if not legacy_presets.is_dir():
        return False
    if new_presets.exists():
        _log.debug('新工作区已存在 presets 目录，跳过旧预设复制')
        return False
    shutil.copytree(legacy_presets, new_presets)
    _log.info(f'预设目录已迁移至新工作区: {new_presets}')
    return True


def _import_legacy_conversations(plugin: Any, legacy_dir: Path) -> bool:
    """从旧 openai_chat_plugin.json 的 `data` 键导入会话数据。

    仅当工作区尚无任何会话数据时导入，避免覆盖新数据；只认 _DATA_KEYS 中的会话字典，
    旧版 `config` 键属于已废弃的配置体系，不再迁移。

    :return: 是否发生了导入
    """
    legacy_data_file = legacy_dir / 'openai_chat_plugin.json'
    if not legacy_data_file.is_file():
        return False
    if _has_existing_conversations(plugin):
        _log.debug('新工作区已存在会话数据，跳过旧会话迁移')
        return False

    try:
        legacy_content = json.loads(legacy_data_file.read_text(encoding='utf-8'))
    except Exception as e:
        _log.error(f'读取旧版数据文件失败，跳过会话迁移: {e}')
        return False

    legacy_data = legacy_content.get('data') if isinstance(legacy_content, dict) else None
    if not isinstance(legacy_data, dict):
        _log.debug('旧数据文件中未发现有效的会话数据，跳过会话迁移')
        return False

    conversations = {
        key: legacy_data[key] for key in _DATA_KEYS if isinstance(legacy_data.get(key), dict)
    }
    if not conversations:
        _log.debug('旧数据文件中未发现有效的会话数据，跳过会话迁移')
        return False

    plugin.data['data'] = {key: {} for key in _DATA_KEYS}
    plugin.data['data'].update(conversations)
    plugin._save_data()
    _log.info(
        '会话数据已迁移至新 data.json'
        f'（群会话 {len(conversations.get("group_conversations", {}))} 个，'
        f'用户会话 {len(conversations.get("user_conversations", {}))} 个）')
    return True


def _has_existing_conversations(plugin: Any) -> bool:
    """判断工作区 data 中是否已存在任意会话数据。"""
    data = plugin.data.get('data')
    if not isinstance(data, dict):
        return False
    return any(data.get(key) for key in _DATA_KEYS)


def _backup_legacy_workspace(legacy_dir: Path) -> None:
    """将旧数据目录改名备份，防止下次启动重复迁移（备份已存在时追加时间戳）。"""
    backup_dir = legacy_dir.with_name('openai_chat_plugin.migrated.bak')
    if backup_dir.exists():
        backup_dir = legacy_dir.with_name(f'openai_chat_plugin.migrated.bak.{int(time.time())}')
    try:
        legacy_dir.rename(backup_dir)
        _log.info(f'旧版数据目录已重命名为 {backup_dir.name}（保留备份，可手动删除）')
    except Exception as e:
        _log.warning(f'旧版数据目录改名失败（不影响迁移结果，下次启动将跳过已迁移项）: {e}')


# ======================================================================
# 三、记忆格式升级：v0.1.0 -> v0.1.4+
# ======================================================================

def needs_memory_migration(plugin: Any) -> bool:
    """检测是否存在需要升级的 legacy 记忆数据。

    逐个读取各预设的 memory.json，只要任一文件中存在 legacy 条目即返回 True。
    单个文件读取失败（权限不足 / JSON 损坏等）时记录日志并跳过，不影响其余文件判断。

    :param plugin: 插件实例
    :return: 是否需要执行记忆格式迁移
    """
    for name, memory_file in _iter_preset_memory_files(plugin):
        memory_data = _load_memory_file(name, memory_file)
        if memory_data is None:
            continue
        if any(_memory_entry_is_legacy(item) for item in memory_data):
            return True
    return False


def migrate_memory_format(plugin: Any) -> None:
    """执行记忆格式迁移：将各预设目录下的 memory.json 升级到 v0.1.4+ 结构。

    由 main.on_load() 在 needs_memory_migration() 为 True 时调用。迁移前会备份原文件。
    注：4.x 时代依赖 config.plugins_config 的「全局配置预设迁移」API 在 NcatBot 5 已移除，
    对应场景由 migrate_legacy_workspace() 覆盖。

    :param plugin: 插件实例
    :return: None
    """
    if _migrate_memory_files(plugin):
        _log.info('已完成 openai_chat_plugin 记忆格式升级')


def _iter_preset_memory_files(plugin: Any):
    """遍历工作区中所有实际存在的预设 memory.json。

    :param plugin: 插件实例（需具有 workspace）
    :yield: (preset_name, memory_file) 元组
    """
    presets_dir = Path(plugin.workspace) / 'presets'
    if not presets_dir.is_dir():
        return
    for preset_dir in presets_dir.iterdir():
        if not preset_dir.is_dir():
            continue
        memory_file = preset_dir / 'memory.json'
        if memory_file.is_file():
            yield preset_dir.name, memory_file


def _load_memory_file(name: str, memory_file: Path) -> list | None:
    """读取 memory.json，失败或格式非列表时返回 None（已记录日志）。"""
    try:
        with open(memory_file, encoding='utf-8') as f:
            memory_data = json.load(f)
    except PermissionError:
        _log.error(f'权限不足，无法读取文件：{memory_file}，跳过该文件的记忆格式检查')
        return None
    except json.JSONDecodeError:
        _log.error(f'{name}/memory.json 不是有效的 JSON 文件，无法进行记忆格式检查')
        return None
    if not isinstance(memory_data, list):
        return None
    return memory_data


def _memory_entry_is_legacy(item: Any) -> bool:
    """检查单项记忆是否为旧版（v0.1.0）格式。

    旧版条目仅含 id(int) 与 content(str)；新版要求包含 from_user / from_group /
    create_time，且 id 为字符串（UUID）。缺少任一新字段即视为 legacy。

    :param item: 单条记忆数据
    """
    if not isinstance(item, dict):
        return False
    if 'content' not in item or 'id' not in item:
        return False
    if not {'from_user', 'from_group', 'create_time'}.issubset(item):
        return True
    return not isinstance(item.get('id'), str) or not isinstance(item.get('content'), str)


def _migrate_memory_files(plugin: Any) -> bool:
    """扫描并迁移所有预设的 memory.json。

    :param plugin: 插件实例
    :return: 是否至少迁移了一个文件
    """
    migrated_any = False
    for name, memory_file in _iter_preset_memory_files(plugin):
        try:
            if _migrate_memory_file(memory_file):
                migrated_any = True
                _log.info(f'记忆格式已迁移：{name}/memory.json')
        except Exception as exc:
            _log.error(f'迁移 `{name}/memory.json` 失败：{exc}')
    return migrated_any


def _migrate_memory_file(memory_file: Path) -> bool:
    """将单个 legacy memory.json 迁移到 v0.1.4+ 格式；无需迁移时返回 False。

    迁移策略：
    - 旧条目补全 from_user / from_group / create_time（来源未知置 0 / -1，时间置 UNKNOWN）
    - id 由旧版整数递增改为 UUID 字符串
    - 迁移前备份原文件为 `<memory_file>.bak.<时间戳>`，保证可回滚

    :param memory_file: memory.json 文件路径
    :return: 是否执行了迁移
    """
    if not memory_file.is_file():
        return False
    memory_data = _load_memory_file(memory_file.name, memory_file)
    if not memory_data or not any(_memory_entry_is_legacy(item) for item in memory_data):
        return False

    migrated: list[dict[str, Any]] = []
    for item in memory_data:
        if not isinstance(item, dict):
            continue
        content = item.get('content', '')
        if not isinstance(content, str):
            continue
        create_time = item.get('create_time', 'UNKNOWN')
        if not isinstance(create_time, str) or not create_time.strip():
            create_time = 'UNKNOWN'
        migrated.append({
            'id': str(uuid.uuid4()),
            'from_user': 0,      # 旧数据无法得知来源：0 表示全局/未知
            'from_group': -1,    # 旧数据无法得知来源：-1 表示私聊/全局
            'create_time': create_time,
            'content': content,
        })

    shutil.copy2(memory_file, f'{memory_file}.bak.{int(time.time())}')
    with open(memory_file, 'w', encoding='utf-8') as f:
        json.dump(migrated, f, ensure_ascii=False, indent=2)
    return True
