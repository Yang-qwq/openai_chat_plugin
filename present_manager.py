# -*- coding: utf-8 -*-
import os

import yaml
from ncatbot.utils.logger import get_log

_log = get_log("openai_chat_plugin.present_manager")


# 尽管这是新代码，但是暂时先使用错误的拼写，之后一并修正
class Present:
    def __init__(self):
        """预设类，包含预设的配置和 prompt 内容
         - config: 预设的配置字典，包含 display_name 等信息
         - prompt: 预设的 prompt 内容，作为 system 消息使用
         - _present_name: 预设名称（用于 fallback）
        """
        _log.debug('新的Present类被创建')
        self.config = None
        self.prompt = None
        self._present_name = None

    def load(self, work_space: os.PathLike | str, present_name: str) -> bool:
        """加载预设

        :param work_space: 工作空间路径
        :param present_name: 预设名称
        :return: 成功返回 True，失败返回 False
        """
        try:
            # 防止路径遍历攻击，确保 present_name 不包含非法路径
            allowed_base = os.path.realpath(os.path.join(work_space, "presents"))
            preset_dir = os.path.realpath(os.path.join(work_space, "presents", present_name))

            if not preset_dir.startswith(allowed_base):
                _log.warning(f"路径遍历攻击被阻止: {present_name}")
                return False

            config_path = os.path.join(preset_dir, "config.yaml")
            prompt_path = os.path.join(preset_dir, "prompt.md")

            if not os.path.exists(config_path) or not os.path.exists(prompt_path):
                return False

            with open(config_path, 'r', encoding='utf-8') as f:
                self.config = yaml.safe_load(f) or {}

            with open(prompt_path, 'r', encoding='utf-8') as f:
                self.prompt = f.read().strip()

            self._present_name = present_name
            _log.debug(f'加载预设完成(present_name={self._present_name})')
            return True
        except Exception as e:
            _log.error(f'加载预设 {present_name} 失败: {e}')
            return False

    def get_display_name(self) -> str:
        """获取预设的显示名称

        :return: 显示名称，如 config 不存在则返回预设名称本身
        """
        if self.config:
            return self.config.get('display_name', self._present_name or '')
        return self._present_name or ''

    def get_prompt(self) -> str:
        """获取预设的 prompt 文本

        :return: prompt 文本
        """
        return self.prompt or ''

    def to_conversations(self) -> list:
        """将预设转换为会话消息列表，供 OpenAI API 使用

        :return: 会话列表（空 prompt 返回空列表，非空返回包含 system 消息的列表）
        """
        if not self.prompt:
            return []
        return [{'role': 'system', 'content': self.prompt}]
