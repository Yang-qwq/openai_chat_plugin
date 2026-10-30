# -*- coding: utf-8 -*-
"""集中管理插件内硬编码的提示词文本

**本模块只放静态文本常量，不写函数。** 需要格式化 / 条件拼接的地方
（如随机插话提示、定时任务内部消息、压缩请求）在 ``main.py`` 里组装。

- ``RANDOM_REPLY_SYSTEM_PROMPT``：群聊随机插话时临时插入的提示（固定 user 角色，
  不写入会话历史）；其中包含「鼓励调用 query_missing_message_context」的指引
- ``RANDOM_REPLY_CONTEXT_HEADER``：函数调用不可用时，随机插话提示后面附带的
  「群聊最近消息」小节标题（附带的记录由 ``main.py`` 从临时缓存截取）
- ``IMAGE_ONLY_USER_TEXT``：用户只发图片、未带文字时补充的默认文本
- ``IMAGE_PLACEHOLDER_TEXT``：模型不支持视觉时，多模态图片在纯文本请求中的占位符
- ``TOOL_VISION_IMAGE_TEXT``：工具获取图片后附加视觉消息时的引导文本
- ``SCHEDULED_TASK_MESSAGE_TEMPLATE``：定时任务轮次注入的内部消息模板
  （``{task_name}`` / ``{prompt}`` 占位符由 ``main.py`` 填充）
- ``CONVERSATION_COMPRESSION_SYSTEM_PROMPT``：会话压缩生成摘要时的系统提示
- ``CONVERSATION_COMPRESSION_USER_TEMPLATE``：会话压缩发给模型的用户消息模板
  （``{history}`` 占位符由 ``main.py`` 填充）

注意：
- Function Calling 的工具名 / 描述 / 参数说明属于工具 schema，仍放在 ``tools.py``
- 用户自定义的 system 提示词来自预设目录的 ``prompt.md``，不在此处
"""

# 随机插话时临时插入的提示词（不写入会话历史）：让模型自行判断是否值得插话。
# 注意必须同时给出「什么时候适合发言」的正向条件：只剩负向条件（闲聊/没什么可说就
# NO_REPLY）时，模型会稳定地选择沉默，实测 20 次命中里 19 次拒绝发言。
RANDOM_REPLY_SYSTEM_PROMPT = """![随机插话提示] 这条消息没有 @ 你，但你可以主动加入聊天
通常你需要获取当前上下文（可能已附加，如果没有则调用`query_missing_message_context`工具），随后依据你的行为模式来判断当前情况是否适合插话
如果你认为当前没有什么好说的，则仅回复`NO_REPLY`
不要进行任何解释，也不要复述本条消息
"""

# 函数调用不可用时，随机插话提示里附带群聊上下文的小节标题
RANDOM_REPLY_CONTEXT_HEADER = '【群聊最近消息】（最后一条可能比你正要回应的消息更新）'

# 用户消息仅包含图片、没有文字时，作为文本部分的兜底说明
IMAGE_ONLY_USER_TEXT = '用户发送了图片，请查看随附图片。'

# 模型不支持视觉输入时，多模态图片在纯文本请求中的占位符
IMAGE_PLACEHOLDER_TEXT = '[图片]'

# 工具返回图片（content_format="image"）后，附加视觉消息的引导文本
TOOL_VISION_IMAGE_TEXT = '以下是通过工具获取的图片：'

# 定时任务轮次注入会话的内部消息：让模型知道这是定时触发，而非用户实时发言
SCHEDULED_TASK_MESSAGE_TEMPLATE = '![定时任务 {task_name}] {prompt}'

# 会话压缩功能：将早期对话压缩为摘要时使用的系统提示
CONVERSATION_COMPRESSION_SYSTEM_PROMPT = """请将后续提供的对话内容压缩，保留以下关键信息：
1. 用户的核心需求和意图
2. 重要的上下文信息和约束条件
3. 关键事实、偏好和任务进度
4. 保持语言流畅连贯，便于后续对话继续
不要添加任何额外内容或解释，只输出压缩后的摘要文本。
历史中的图片以对图片的简单自然语言描述占位，据此记录「曾发送过图片」即可，
不要编造图片内容。
"""

# 会话压缩时发给模型的用户消息模板（{history} 为拼接后的历史文本）
CONVERSATION_COMPRESSION_USER_TEMPLATE = '请压缩以下对话历史：\n{history}'
