# -*- coding: utf-8 -*-
"""
数据迁移/更新实现模块

1) 预设配置迁移（旧版 global config -> `presents/` 目录）
2) 记忆格式迁移（旧 memory.json -> v0.1.4+ memory.json）

预设数据结构（v1.0+）：
openai_chat_plugin/
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
from typing import Any, Dict, List

import yaml
from ncatbot.utils import config
from ncatbot.utils.logger import get_log

_log = get_log('openai_chat_plugin.update')


def _should_create_files(presents_dir: str, presents: Dict[str, Dict[str, Any]]) -> bool:
    """判断当前文件结构是否需要创建/更新。

    :param presents_dir: 预设数据目录路径
    :param presents: 来自全局配置的预设数据字典
    :return: bool
    """
    if not presents:
        return False

    # 如果总目录都不存在，肯定需要更新
    if not os.path.exists(presents_dir):
        return True

    # 任意一个预设缺少目录或核心文件时认为需要更新
    for name in presents.keys():
        present_dir = os.path.join(presents_dir, name)
        config_path = os.path.join(present_dir, 'config.yaml')
        prompt_path = os.path.join(present_dir, 'prompt.md')
        if not (os.path.exists(present_dir) and os.path.isfile(config_path) and os.path.isfile(prompt_path)):
            return True

    return False


def is_need_update(plugin: Any) -> bool:
    """判断是否需要执行数据迁移。

    判断依据：

    v0.0.x -> v0.1.0 的迁移需要满足以下条件：
    - 全局配置中存在 openai_chat_plugin.presents；
    - 且数据目录下尚未为所有预设生成对应的 config.yaml 与 prompt.md。

    v0.1.0 -> v0.1.4 的迁移需要满足以下条件：
    - 预设目录下存在 legacy memory.json；
    - 且 memory.json 中存在 legacy 数据。

    :param plugin:
    :return: bool
    """
    preset_need_update = False

    # 如果全局配置中没有预设数据，则先只判断记忆格式
    if config.plugins_config is not None:
        plugin_cfg = config.plugins_config.get('openai_chat_plugin') or {}
        presents = plugin_cfg.get('presents') or {}

        # 如果配置中没有预设数据，也不需要更新
        if presents:
            presents_dir = os.path.join(plugin.work_space.path.as_posix(), 'presents')
            preset_need_update = _should_create_files(presents_dir, presents)

    memory_need_update = _should_update_memory_format(plugin)
    return preset_need_update or memory_need_update


def _memory_entry_is_legacy(item: Any) -> bool:
    """检查单项记忆是否为旧版记忆格式

    旧版 memory.json 仅包含：
    - id: int
    - content: str

    新版 memory.json 结构要求：参见tools.py

    :param item:
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
    if not isinstance(item.get('content'), str):
        return True

    return False


def _should_update_memory_format(plugin: Any) -> bool:
    """检测 presents 目录下是否存在 legacy memory.json。

    :param plugin:
    :return: bool
    """
    presents_dir = os.path.join(plugin.work_space.path.as_posix(), 'presents')
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

            try:
                with open(memory_file, 'r', encoding='utf-8') as f:
                    memory_data = json.load(f)
            except PermissionError:
                _log.error('权限不足，无法读取文件：{memory_file}，跳过该文件的记忆格式检查')
            except json.decoder.JSONDecodeError:
                _log.error(f'{name}/memory.json 不是有效的 JSON 文件，无法进行记忆格式检查')

            if isinstance(memory_data, list) and any(_memory_entry_is_legacy(x) for x in memory_data):
                return True
    except Exception:
        return False

    return False


def _migrate_memory_file(preset_memory_file: str) -> bool:
    """将 legacy memory.json 迁移到 v0.1.4+ 格式。

    :param preset_memory_file:
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

    migrated: List[Dict[str, Any]] = []
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


def _build_prompt(conversations: Any) -> str:
    """
    从 conversations 列表中提取 system 消息并构造 prompt 文本。

    - 如果存在多条 system 消息，则使用分隔线拼接；
    - 如果不存在 system 消息，则返回空字符串。
    """
    if not isinstance(conversations, list):
        return ''

    system_contents = []
    for msg in conversations:
        if not isinstance(msg, dict):
            continue
        if msg.get('role') != 'system':
            continue
        content = msg.get('content')
        if isinstance(content, str) and content.strip():
            system_contents.append(content.strip())

    if not system_contents:
        return ''

    if len(system_contents) == 1:
        return system_contents[0]

    separator = '\n\n---\n\n'
    return separator.join(system_contents)


def _write_present_files(presents_dir: str, name: str, present_cfg: Dict[str, Any]) -> None:
    """
    为单个预设生成/更新目录及文件。
    """
    present_dir = os.path.join(presents_dir, name)
    os.makedirs(present_dir, exist_ok=True)

    display_name = present_cfg.get('display_name', name)
    conversations = present_cfg.get('conversations') or []

    # 写入 config.yaml
    config_content = yaml.safe_dump({
        'version': 1,
        'display_name': display_name
    }, allow_unicode=True)
    config_path = os.path.join(present_dir, 'config.yaml')
    with open(config_path, 'w', encoding='utf-8') as f:
        f.write(config_content)

    # 写入 prompt.md（仅包含 system 提示词）
    prompt_text = _build_prompt(conversations)
    prompt_path = os.path.join(present_dir, 'prompt.md')
    with open(prompt_path, 'w', encoding='utf-8') as f:
        f.write(prompt_text)


def update_data(plugin: Any) -> None:
    """执行数据迁移：
    - 为每个预设创建独立目录；
    - 为每个预设写入 config.yaml 与 prompt.md；
    - 将各预设目录下的 memory.json 升级到 v0.1.4+ 结构；
    - 日志提醒用户删除旧的配置文件片段。

    :param plugin:
    :return: None
    """
    # 1) 预设迁移：global config -> presents/
    preset_migrated_any = _migrate_presents_from_global_config(plugin)
    if preset_migrated_any:
        # 迁移完成后提醒用户清理旧配置
        _log.info(
            '已将 openai_chat_plugin 预设迁移到数据目录的 `presents/` 中，'
            '请删除全局配置文件中 `plugins_config.openai_chat_plugin.presents` 相关旧配置，以避免混淆'
        )

    # 2) 记忆迁移：legacy memory.json -> v0.1.4+ memory.json
    memory_migrated_any = _migrate_memory_files(plugin)
    if memory_migrated_any:
        _log.info('已完成 openai_chat_plugin 记忆格式升级')


def _migrate_presents_from_global_config(plugin: Any) -> bool:
    """将旧版全局 config 中的 openai_chat_plugin.presents 迁移到 presents/ 目录。

    :param plugin:
    :return: bool
    """
    presents: Dict[str, Dict[str, Any]] = {}
    if config.plugins_config is not None:
        plugin_cfg = config.plugins_config.get('openai_chat_plugin') or {}
        presents = plugin_cfg.get('presents') or {}

    if not presents:
        _log.debug('未在配置中发现 openai_chat_plugin 预设，无需迁移预设数据')
        return False

    presents_dir = os.path.join(plugin.work_space.path.as_posix() + '/', 'presents')
    if not _should_create_files(presents_dir, presents):
        _log.debug('检测到预设数据目录已存在且完整，跳过预设迁移')
        return False

    os.makedirs(presents_dir, exist_ok=True)
    preset_migrated_any = False

    for name, present_cfg in presents.items():
        try:
            _write_present_files(presents_dir, name, present_cfg or {})
            preset_migrated_any = True
            _log.info(f'预设 `{name}` 已迁移至数据目录')
        except Exception as exc:  # 保守起见，避免单个预设失败中断全部迁移
            _log.error(f'迁移预设 `{name}` 时发生错误：{exc}')

    return preset_migrated_any


def _migrate_memory_files(plugin: Any) -> bool:
    """扫描 presents/<present_name>/memory.json 并进行 legacy -> v0.1.4+ 迁移。

    :param plugin:
    :return: bool
    """
    presents_dir = os.path.join(plugin.work_space.path.as_posix(), 'presents')
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
