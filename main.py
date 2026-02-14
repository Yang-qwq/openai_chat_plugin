# -*- coding: utf-8 -*-
import json
import os
import shlex

from ncatbot.core import BaseMessage, GroupMessage, PrivateMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI

from .present_manager import get_preset_display_name, load_preset
from .tools import memory_common_tool, memory_delete_tool, tools
from .update import is_need_update, update_data

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log('openai_chat_plugin')  # 日志记录器

DEFAULT_PRESENT_NAME = 'default'  # 默认预设名称

ADMIN_HELP_TEXT = """OpenAI Chat Plugin 管理员命令帮助：

/chat-admin set-present <name> [group:<id>|user:<id>] - 设置预设（管理员功能）
/chat-admin reset [group:<id>|user:<id>] - 重置会话（管理员功能）
/chat-admin help - 显示此帮助信息

示例：
/chat-admin set-present MyPresent
/chat-admin set-present MyPresent group:1919810
/chat-admin set-present MyPresent user:114514
/chat-admin reset
/chat-admin reset group:1919810
/chat-admin reset user:114514

注意：这些命令仅限管理员使用，可以跨群聊设置预设。"""

USER_HELP_TEXT = """OpenAI Chat Plugin 用户命令帮助：

/chat set-present <name> - 设置预设（仅限当前用户/群组）
/chat reset - 重置当前会话
/chat help - 显示此帮助信息

示例：
/chat set-present MyPresent
/chat reset
/chat help
"""


class OpenAIChatPlugin(BasePlugin):
    name = 'OpenAIChatPlugin'  # 插件名
    version = '0.1.1'  # 插件版本

    async def admin_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理管理员命令事件

        :param event: 事件对象
        :return:
        """
        # 替换消息中的转义符，如\\n -> \n
        replaced_message = event.raw_message.replace("\\n", "\n")

        # 解析命令
        command = shlex.split(replaced_message)

        # 检测是否为管理员命令
        if command[0] != '/chat-admin':
            return

        # 检测命令长度
        # 如/chat-admin
        if len(command) == 1:
            # 显示帮助信息
            await event.reply_text(ADMIN_HELP_TEXT)
            return

        elif len(command) > 1:
            # 功能：选择多预设（管理员功能）
            # 例如：/chat-admin set-present <name> [group:<id>|user:<id>]
            if command[1] == 'set-present':
                if len(command) < 3:
                    await event.reply_text("请提供预设名称。")
                    return

                present_name = command[2]
                target = None

                # 检查是否指定了目标
                if len(command) > 3:
                    target = command[3]

                # 设置预设
                if target is None:  # 没有指定目标，使用默认配置
                    conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                    if conversations is None:
                        await event.reply_text(f"预设 {present_name} 不存在。")
                        return

                    display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)

                    if event.message_type == 'group':
                        self.data['data']['group_conversations'][event.group_id] = conversations.copy()
                    else:
                        self.data['data']['user_conversations'][event.user_id] = conversations.copy()

                    await event.reply_text(f"已设置当前预设为: {present_name}({display_name})")
                else:  # 指定了目标
                    conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                    if conversations is None:
                        await event.reply_text(f"预设 {present_name} 不存在。")
                        return

                    display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)

                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self.data['data']['group_conversations'][group_id] = conversations.copy()
                            await event.reply_text(f"已为群组 {group_id} 设置预设: {present_name}({display_name})")
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self.data['data']['user_conversations'][user_id] = conversations.copy()
                            await event.reply_text(f"已为用户 {user_id} 设置预设: {present_name}({display_name})")
                        else:
                            await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")
                    except (ValueError, IndexError):
                        await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")

            # 功能：重置会话（管理员功能）
            # 例如：/chat-admin reset [group:<id>|user:<id>]
            elif command[1] == 'reset':
                target = None

                # 检查是否指定了目标
                if len(command) > 2:
                    target = command[2]

                # 加载默认预设
                default_conversations = load_preset(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME)
                if default_conversations is None:
                    await event.reply_text("默认预设不存在，无法重置会话。")
                    return

                if target is None:
                    # 重置当前会话
                    if event.message_type == 'group':
                        self.data['data']['group_conversations'][event.group_id] = default_conversations.copy()
                    else:
                        self.data['data']['user_conversations'][event.user_id] = default_conversations.copy()
                    await event.reply_text("已重置当前会话。")
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self.data['data']['group_conversations'][group_id] = default_conversations.copy()
                            await event.reply_text(f"已重置群组 {group_id} 的会话。")
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self.data['data']['user_conversations'][user_id] = default_conversations.copy()
                            await event.reply_text(f"已重置用户 {user_id} 的会话。")
                        else:
                            await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")
                    except (ValueError, IndexError):
                        await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")

            # 功能：显示管理员帮助信息
            elif command[1] == 'help':
                await event.reply_text(ADMIN_HELP_TEXT)
                return

            else:
                await event.reply_text("未知管理员命令，请使用 /chat-admin help 查看帮助信息。")

    async def user_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理用户命令事件

        :param event: 事件对象
        :return:
        """

        # 替换消息中的转义符，如\\n -> \n
        replaced_message = event.raw_message.replace("\\n", "\n")

        # 解析命令
        command = shlex.split(replaced_message)

        # 检测是否为用户命令
        if command[0] != '/chat':
            return

        # 检测命令长度
        # 如/chat
        if len(command) == 1:
            # 显示帮助信息
            await event.reply_text(USER_HELP_TEXT)
            return

        elif len(command) > 1:
            if event.message_type == 'group':  # 群消息
                conversation_dict = 'group_conversations'
            else:
                conversation_dict = 'user_conversations'

            # 功能：选择预设（仅限当前用户/群组）
            # 例如：/chat set-present <name>
            if command[1] == 'set-present':
                if len(command) < 3:
                    await event.reply_text("请提供预设名称。")
                    return

                present_name = command[2]

                # 设置预设（仅限当前用户/群组）
                conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                if conversations is None:
                    await event.reply_text(f"预设 {present_name} 不存在。")
                    return

                display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)
                self.data['data'][conversation_dict][
                    event.group_id if event.message_type == 'group' else event.user_id] = conversations.copy()
                await event.reply_text(f"已设置当前预设为: {present_name}({display_name})")

            # 功能：重置当前会话
            # 例如：/chat reset
            elif command[1] == 'reset':
                # 重置当前会话
                default_conversations = load_preset(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME)
                if default_conversations is None:
                    await event.reply_text("默认预设不存在，无法重置会话。")
                    return

                self.data['data'][conversation_dict][
                    event.group_id if event.message_type == 'group' else event.user_id] = default_conversations.copy()
                await event.reply_text("已重置当前会话。")
                return

            # 功能：显示帮助信息
            elif command[1] == 'help':
                await event.reply_text(USER_HELP_TEXT)

            else:
                await event.reply_text("未知命令，请使用 /chat help 查看帮助信息。")

    async def on_load(self):
        self.register_config(
            "api_key", description="你的OpenAI API Key", default="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx",
            value_type="str"
        )
        self.register_config(
            "model", description="使用的模型", default="openai/gpt-4o-mini", value_type="str"
        )
        self.register_config(
            "base_url", description="OpenAI API 基础URL", default="https://api.openai.com/v1", value_type="str"
        )
        self.register_config(
            "insert_userdata_as_prefix",
            description="是否修改用户消息，在消息前添加用户名作为前缀（例如：'Alice: 你好'）", value_type="bool",
            default=False
        )
        self.register_config(
            "must_at_bot", description="是否必须@机器人才能触发对话",
            value_type="bool",
            default=True
        )
        self.register_config(
            "max_conversations", description="每个会话的最大消息数", value_type="int",
            default=21
        )
        self.register_config(
            "enable_builtin_function_calling", description="是否启用内置函数调用功能", value_type="bool",
            default=False
        )
        self.register_config(
            "allow_access_memory", description="是否允许AI记录和访问会话历史（内置函数调用功能需要开启）",
            default=False, value_type="bool"
        )
        self.register_config("max_retries_times",
                             description="当启用内置函数调用功能时，模型想要调用工具后重新生成回复的最大重试次数",
                             value_type="int", default=10)
        self.register_config(
            "is_configured", description="插件是否已配置",
            value_type="bool",
            default=False
        )

        self.register_user_func("用户命令", self.user_command_handler, prefix='/chat', description="设置预设、重置会话",
                                usage="/chat <set-present|reset|help> [present_name]",
                                examples=[
                                    "/chat set-present MyPresent",  # 设置预设
                                    "/chat reset",  # 重置当前会话
                                    "/chat help"  # 显示帮助信息
                                ])

        self.register_admin_func("管理员命令", self.admin_command_handler, prefix='/chat-admin',
                                 description="跨群组/用户设置预设、重置会话",
                                 usage="/chat-admin <set-present|reset|help> [args]",
                                 examples=[
                                     "/chat-admin set-present MyPresent",  # 设置预设
                                     "/chat-admin set-present MyPresent group:1919810",  # 跨群组设置预设
                                     "/chat-admin set-present MyPresent user:114514",  # 跨用户设置预设
                                     "/chat-admin reset",  # 重置当前会话
                                     "/chat-admin reset group:1919810",  # 跨群组重置会话
                                     "/chat-admin reset user:114514",  # 跨用户重置会话
                                     "/chat-admin help"  # 显示帮助信息
                                 ])

        # 初始化持久化数据
        if 'data' not in self.data:
            self.data['data'] = {
                'group_conversations': {},
                'user_conversations': {}
            }

        # 检查是否已存在默认预设
        if os.path.exists(self.work_space.path.as_posix() + '/presents/default/config.yaml') and os.path.exists(
                self.work_space.path.as_posix() + '/presents/default/prompt.md'):
            _log.debug("检测到默认预设已存在，跳过创建默认预设。")
        else:
            # 创建默认预设
            default_preset_dir = os.path.join(self.work_space.path.as_posix(), 'presents', DEFAULT_PRESENT_NAME)
            os.makedirs(default_preset_dir, exist_ok=True)

            default_config_path = os.path.join(default_preset_dir, 'config.yaml')
            default_prompt_path = os.path.join(default_preset_dir, 'prompt.md')

            if not os.path.exists(default_config_path):
                with open(default_config_path, 'w', encoding='utf-8') as f:
                    f.write("display_name: 默认预设\n")

            # 仅创建`prompt.md`文件，不写入任何内容，用户可以自行编辑添加 system 提示词
            if not os.path.exists(default_prompt_path):
                with open(default_prompt_path, 'w', encoding='utf-8') as f:
                    f.write("")  # 创建一个空的 prompt.md 文件
                _log.info("已创建默认预设的 prompt.md 文件，请编辑该文件添加 system 提示词。")

        # 判断是否已配置
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件。")

        # 在插件加载时尝试迁移旧版预设配置到数据目录
        try:
            if is_need_update(self):
                _log.info("检测到需要迁移的预设配置，正在迁移数据...")
                update_data(self)
            else:
                _log.debug("未检测到需要迁移的预设配置，跳过数据迁移。")
        except Exception as e:  # 迁移失败不应阻塞插件整体加载
            _log.error(f"迁移 openai_chat_plugin 预设数据失败：{e}")

        # 检查默认预设是否存在
        default_preset = load_preset(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME)
        if default_preset is None:
            _log.error("默认预设不存在，请确保数据目录中存在 presents/default/ 目录及其配置文件。")
            # 设置`is_configured`为False
            self.config['is_configured'] = False

        # 检查并修剪所有现有会话，确保符合新的配置限制
        self._trim_all_conversations()

    def _trim_conversation_if_needed(self, conversation):
        """检查会话长度，如果超过限制则删除最早的非system消息（保护最近的对话）

        :param conversation: 会话列表
        :return: None
        """
        while len(conversation) > self.config['max_conversations']:
            # 找到最早的非system消息的索引（从前往后找第一个非system消息）
            earliest_non_system_index = None
            for i, msg in enumerate(conversation):
                if msg.get('role') != 'system':
                    earliest_non_system_index = i
                    break

            # 如果找到了非system消息，删除它
            if earliest_non_system_index is not None:
                conversation.pop(earliest_non_system_index)
                _log.debug(f"会话长度超过限制({self.config['max_conversations']})，已删除最早的非system消息")
            else:
                # 如果没有找到非system消息，说明所有消息都是system，无法删除
                _log.warning(f"会话长度超过限制({self.config['max_conversations']})，但所有消息都是system，无法删除")
                break

    def _trim_all_conversations(self):
        """检查并修剪所有现有会话，确保符合新的配置限制"""
        # 检查群组会话
        for group_id, conversation in self.data['data']['group_conversations'].items():
            original_length = len(conversation)
            self._trim_conversation_if_needed(conversation)
            if len(conversation) < original_length:
                _log.info(f"群组 {group_id} 的会话已从 {original_length} 条消息修剪到 {len(conversation)} 条消息")

        # 检查用户会话
        for user_id, conversation in self.data['data']['user_conversations'].items():
            original_length = len(conversation)
            self._trim_conversation_if_needed(conversation)
            if len(conversation) < original_length:
                _log.info(f"用户 {user_id} 的会话已从 {original_length} 条消息修剪到 {len(conversation)} 条消息")

    async def _handle_message(self, event: GroupMessage | PrivateMessage | BaseMessage):
        """处理消息事件

        :param event: 事件对象
        :return: None
        """

        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理
        if event.raw_message.strip().startswith('/'):
            _log.debug("检测到命令消息，跳过聊天处理以避免重复触发。")
            return

        # 检查是否已配置插件
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件后再使用。")
            return

        client = OpenAI(
            api_key=self.config['api_key'],
            base_url=self.config['base_url']
        )

        user_message = f"{event.sender.nickname}({event.sender.user_id}): {event.raw_message}" if self.config[
            'insert_userdata_as_prefix'] else event.raw_message

        if event.message_type == 'group':  # 群消息
            conversation_dict = 'group_conversations'

            # 检查是否必须@机器人才能触发对话
            if self.config['must_at_bot']:
                _at_bot = False
                for msg in event.message:
                    if 'at' in msg['type'] and msg['data']['qq'] == config.bt_uin:
                        _log.debug("群消息已@机器人，处理该消息。")
                        _at_bot = True
                        break  # 找到@机器人消息后退出循环
                if not _at_bot:
                    _log.debug("群消息未@机器人，忽略该消息。")
                    return

            # 检查群会话是否存在
            if event.group_id not in self.data['data'][conversation_dict]:
                default_conversations = load_preset(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME)
                if default_conversations is None:
                    _log.error("默认预设不存在，无法初始化会话。")
                    return
                self.data['data'][conversation_dict][event.group_id] = default_conversations.copy()

            # 在添加用户消息前检查会话长度
            conversation = self.data['data'][conversation_dict][event.group_id]
            self._trim_conversation_if_needed(conversation)

            self.data['data'][conversation_dict][event.group_id].append({"role": "user", "content": user_message})
        else:
            conversation_dict = 'user_conversations'
            if event.user_id not in self.data['data'][conversation_dict]:  # 私聊消息
                default_conversations = load_preset(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME)
                if default_conversations is None:
                    _log.error("默认预设不存在，无法初始化会话。")
                    return
                self.data['data'][conversation_dict][event.user_id] = default_conversations.copy()

            # 在添加用户消息前检查会话长度
            conversation = self.data['data'][conversation_dict][event.user_id]
            self._trim_conversation_if_needed(conversation)

            # 添加用户消息到会话
            self.data['data'][conversation_dict][event.user_id].append({"role": "user", "content": user_message})

        try:
            current_retries_times = 0

            # 如果启用了内置函数调用功能，则在模型想要调用工具时会循环执行工具调用并获取结果，直到模型不再想要调用工具或达到最大重试次数为止
            while current_retries_times < self.config['max_retries_times']:
                response = client.chat.completions.create(
                    model=self.config['model'],
                    messages=self.data['data'][conversation_dict][
                        event.group_id if event.message_type == 'group' else event.user_id],
                    tools=tools if self.config['enable_builtin_function_calling'] else None,
                    tool_choice="auto" if self.config['enable_builtin_function_calling'] else "none",
                )

                _log.debug(
                    f'请求尝试：{current_retries_times + 1}/{self.config["max_retries_times"]}，'
                    f'模型回复: {response.choices[0].message.content}, '
                    f'finish_reason: {response.choices[0].finish_reason}, '
                    f'tool_calls: {response.choices[0].message.tool_calls}'
                )

                # 模型是否主动停止生成回复
                if response.choices[0].finish_reason == 'stop':
                    _log.debug('模型回复已完成，无需继续处理工具调用。')
                    break

                # 检查模型是否想要调用工具
                if self.config['enable_builtin_function_calling']:
                    if response.choices[0].message.tool_calls:
                        current_retries_times += 1

                        # 发送思考中的消息（可选）
                        # 先检查回复内容是否为空
                        # if not response.choices[0].message.content == '':
                        #     if event.message_type == 'group':
                        #         await self.api.post_group_msg(event.group_id, response.choices[0].message.content)
                        #     else:
                        #         await self.api.post_private_msg(event.user_id, response.choices[0].message.content)

                        # 处理每个工具调用请求
                        for tool_call in response.choices[0].message.tool_calls:
                            tool_name = tool_call.function.name
                            tool_args = json.loads(tool_call.function.arguments)

                            if tool_name == 'memory_common_tool':
                                result = memory_common_tool(self.work_space.path.as_posix(), **tool_args)
                            elif tool_name == 'memory_delete_tool':
                                result = memory_delete_tool(self.work_space.path.as_posix(), **tool_args)
                            else:
                                _log.warning(f"未知工具调用请求: {tool_name}")
                                continue

                            # 将工具调用结果添加到会话中，供模型后续生成回复时参考
                            self.data['data'][conversation_dict][
                                event.group_id if event.message_type == 'group' else event.user_id].append(
                                {"tool_call_id": tool_call.id, "role": "tool", "name": tool_name, "content": result})
                    else:
                        break

            reply_message = response.choices[0].message.content

            # 回复消息
            await event.reply(reply_message)

            # 添加AI回复到会话
            self.data['data'][conversation_dict][
                event.group_id if event.message_type == 'group' else event.user_id].append(
                {"role": "assistant", "content": reply_message})

            # 添加AI回复后再次检查会话长度
            conversation = self.data['data'][conversation_dict][
                event.group_id if event.message_type == 'group' else event.user_id]
            self._trim_conversation_if_needed(conversation)
        except Exception as e:
            _log.error(f'API 调用失败: {e.__class__.__name__}: {e}')
            await event.reply("抱歉，插件出现内部错误，请稍后再试。")

    @bot.group_event()
    async def on_group_message(self, event: GroupMessage):
        """处理群消息事件"""
        await self._handle_message(event)

    @bot.private_event()
    async def on_private_message(self, event: BaseMessage):
        """处理私聊消息事件"""
        await self._handle_message(event)
