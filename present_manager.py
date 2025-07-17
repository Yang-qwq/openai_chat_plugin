# -*- coding: utf-8 -*-
import os
import yaml

from ncatbot.utils.logger import get_log

_log = get_log("openai_chat_plugin.present_manager")


def load_preset(work_space: os.PathLike | str, present_name: str):
    """从数据目录加载预设配置

    :param work_space: 工作空间对象或路径
    :param present_name: 预设名称
    :return: 会话列表（包含system消息），如果预设不存在则返回None
    """
    try:
        preset_dir = os.path.join(work_space, "presents", present_name)
        config_path = os.path.join(preset_dir, "config.yaml")
        prompt_path = os.path.join(preset_dir, "prompt.md")

        # 检查文件是否存在
        if not os.path.exists(config_path) or not os.path.exists(prompt_path):
            return None

        # 读取 prompt.md 作为 system 消息内容
        with open(prompt_path, 'r', encoding='utf-8') as f:
            prompt_content = f.read().strip()

        # 如果 prompt_content 为空，就返回一个空列表
        if not prompt_content:
            return []
        else:  # 返回会话列表，包含一个 system 消息
            return [{"role": "system", "content": prompt_content}]
    except Exception as e:
        _log.error(f"加载预设 {present_name} 失败: {e}")
        return None


def get_preset_display_name(work_space: os.PathLike | str, present_name: str):
    """获取预设的显示名称

    :param work_space: 工作空间对象或路径
    :param present_name: 预设名称
    :return: 显示名称，如果不存在则返回预设名称本身
    """
    try:
        config_path = os.path.join(work_space, "presents", present_name, "config.yaml")

        if not os.path.exists(config_path):
            return present_name

        with open(config_path, 'r', encoding='utf-8') as f:
            preset_config = yaml.safe_load(f)

        return preset_config.get('display_name', present_name)
    except Exception as e:
        _log.error(f"获取预设 {present_name} 的显示名称失败: {e}")
        return present_name
