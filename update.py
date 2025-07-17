# -*- coding: utf-8 -*-
"""
负责将旧版配置中的预设数据迁移到插件的数据目录中，形成独立的预设数据结构：

openai_chat_plugin/
| -- presents/
    | -- <present_name>/
        | -- config.yaml
        | -- prompt.md

说明：
- config.yaml 仅包含版本号和展示名称，例如：
    version: 1
    display_name: "默认助手"
- prompt.md 存放该预设的 system 提示词（如存在多个 system 消息，将按顺序以分隔线拼接）
"""

import os
from typing import Any, Dict

import yaml
from ncatbot.utils import config
from ncatbot.utils.logger import get_log

_log = get_log("openai_chat_plugin.update")


def _should_create_files(presents_dir: str, presents: Dict[str, Dict[str, Any]]) -> bool:
    """
    判断当前文件结构是否需要创建/更新。
    """
    if not presents:
        return False

    # 如果总目录都不存在，肯定需要更新
    if not os.path.exists(presents_dir):
        return True

    # 任意一个预设缺少目录或核心文件时认为需要更新
    for name in presents.keys():
        present_dir = os.path.join(presents_dir, name)
        config_path = os.path.join(present_dir, "config.yaml")
        prompt_path = os.path.join(present_dir, "prompt.md")
        if not (os.path.exists(present_dir) and os.path.isfile(config_path) and os.path.isfile(prompt_path)):
            return True

    return False


def is_need_update(plugin: Any) -> bool:
    """
    判断是否需要执行数据迁移。

    判断依据：

    v0.0.x -> v0.1.0 的迁移需要满足以下条件：
    - 全局配置中存在 openai_chat_plugin.presents；
    - 且数据目录下尚未为所有预设生成对应的 config.yaml 与 prompt.md。
    """
    # 如果全局配置中没有预设数据，直接返回不需要更新
    if config.plugins_config is None:
        return False
    else:
        plugin_cfg = config.plugins_config.get("openai_chat_plugin") or {}
        presents = plugin_cfg.get("presents") or {}

        # 如果配置中没有预设数据，也不需要更新
        if not presents:
            return False

        presents_dir = os.path.join(plugin.work_space.path.as_posix(), "presents")
        return _should_create_files(presents_dir, presents)


def _build_prompt(conversations: Any) -> str:
    """
    从 conversations 列表中提取 system 消息并构造 prompt 文本。

    - 如果存在多条 system 消息，则使用分隔线拼接；
    - 如果不存在 system 消息，则返回空字符串。
    """
    if not isinstance(conversations, list):
        return ""

    system_contents = []
    for msg in conversations:
        if not isinstance(msg, dict):
            continue
        if msg.get("role") != "system":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            system_contents.append(content.strip())

    if not system_contents:
        return ""

    if len(system_contents) == 1:
        return system_contents[0]

    separator = "\n\n---\n\n"
    return separator.join(system_contents)


def _write_present_files(presents_dir: str, name: str, present_cfg: Dict[str, Any]) -> None:
    """
    为单个预设生成/更新目录及文件。
    """
    present_dir = os.path.join(presents_dir, name)
    os.makedirs(present_dir, exist_ok=True)

    display_name = present_cfg.get("display_name", name)
    conversations = present_cfg.get("conversations") or []

    # 写入 config.yaml
    config_content = yaml.safe_dump({
        "version": 1,
        "display_name": display_name
    }, allow_unicode=True)
    config_path = os.path.join(present_dir, "config.yaml")
    with open(config_path, "w", encoding="utf-8") as f:
        f.write(config_content)

    # 写入 prompt.md（仅包含 system 提示词）
    prompt_text = _build_prompt(conversations)
    prompt_path = os.path.join(present_dir, "prompt.md")
    with open(prompt_path, "w", encoding="utf-8") as f:
        f.write(prompt_text)


def update_data(plugin: Any) -> None:
    """
    执行数据迁移：
    - 为每个预设创建独立目录；
    - 为每个预设写入 config.yaml 与 prompt.md；
    - 日志提醒用户删除旧的配置文件片段。
    """
    plugin_cfg = config.plugins_config.get("openai_chat_plugin") or {}
    presents = plugin_cfg.get("presents") or {}
    if not presents:
        _log.debug("未在配置中发现任何 openai_chat_plugin 预设，无需迁移数据。")
        return

    presents_dir = os.path.join(plugin.work_space.path.as_posix() + '/', "presents")

    if not _should_create_files(presents_dir, presents):
        _log.debug("检测到预设数据目录已存在且完整，跳过迁移。")
        return

    os.makedirs(presents_dir, exist_ok=True)

    for name, present_cfg in presents.items():
        try:
            _write_present_files(presents_dir, name, present_cfg or {})
            _log.info(f"预设 `{name}` 已迁移至数据目录。")
        except Exception as exc:  # 保守起见，避免单个预设失败中断全部迁移
            _log.error(f"迁移预设 `{name}` 时发生错误：{exc}")

    # 迁移完成后提醒用户清理旧配置
    _log.info(
        "已将 openai_chat_plugin 预设迁移到数据目录的 `presents/` 中，"
        "请删除全局配置文件中 `plugins_config.openai_chat_plugin.presents` 相关旧配置，以避免混淆。"
    )
