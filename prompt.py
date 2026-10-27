# -*- coding: utf-8 -*-
"""集中管理插件内硬编码的提示词文本

所有会作为消息内容发给模型的自然语言提示集中在本模块，调整提示词时只需改这里：

- ``RANDOM_REPLY_SYSTEM_PROMPT``：群聊随机插话时临时插入的提示（固定 user 角色，
  不写入会话历史）
- ``IMAGE_ONLY_USER_TEXT``：用户只发图片、未带文字时补充的默认文本
- ``IMAGE_PLACEHOLDER_TEXT``：模型不支持视觉时，多模态图片在纯文本请求中的占位符
- ``TOOL_VISION_IMAGE_TEXT``：工具获取图片后附加视觉消息时的引导文本
- ``SCHEDULED_TASK_MESSAGE_TEMPLATE``：定时任务轮次注入的内部消息模板

注意：
- Function Calling 的工具名 / 描述 / 参数说明属于工具 schema，仍放在 ``tools.py``
- 用户自定义的 system 提示词来自预设目录的 ``prompt.md``，不在此处
"""

# 随机插话时临时插入的提示词（不写入会话历史）：让模型自行判断是否值得插话
RANDOM_REPLY_SYSTEM_PROMPT = (
    '现在你处于群聊随机插话模式：这条消息并没有 @ 你，是否参与完全由你决定。\n'
    '只有当内容与你的设定相关、且你确实有值得补充的内容时才简短发言；'
    '如果只是日常闲聊、你没什么可说的，或插话会显得突兀，请只输出 NO_REPLY（不要输出任何其他内容）。\n'
    '即使你调用过工具，若仍然没有值得补充的内容，也请只输出 NO_REPLY，不要因为用过工具就强行发言。\n'
    '不要复述本条提示，也不要解释你的判断。'
)

# 用户消息仅包含图片、没有文字时，作为文本部分的兜底说明
IMAGE_ONLY_USER_TEXT = '用户发送了图片，请查看随附图片。'

# 模型不支持视觉输入时，多模态图片在纯文本请求中的占位符
IMAGE_PLACEHOLDER_TEXT = '[图片]'

# 工具返回图片（content_format="image"）后，附加视觉消息的引导文本
TOOL_VISION_IMAGE_TEXT = '以下是通过工具获取的图片：'

# 定时任务轮次注入会话的内部消息：让模型知道这是定时触发，而非用户实时发言
SCHEDULED_TASK_MESSAGE_TEMPLATE = '[定时任务 {task_name}] {prompt}'


def build_scheduled_task_message(task_name: str, prompt: str) -> str:
    """按模板生成定时任务的内部消息。

    :param task_name: 任务名（不含插件前缀）
    :param prompt: 创建任务时保存的提示内容
    :return: 注入会话的 user 消息文本
    """
    return SCHEDULED_TASK_MESSAGE_TEMPLATE.format(task_name=task_name, prompt=prompt)
