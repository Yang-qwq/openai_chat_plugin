# -*- coding: utf-8 -*-
import shlex

from ncatbot.core import BaseMessage, GroupMessage, PrivateMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log("openai_chat_plugin")  # 日志记录器


class OpenAIChatPlugin(BasePlugin):
    name = "OpenAIChatPlugin"  # 插件名
    version = "0.0.4"  # 插件版本

    async def admin_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理管理员命令事件"""
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
            # await event.reply_text(
            #     "管理员命令无效，请执行\n/chat-admin help OpenAIChatPlugin\n获得帮助信息。"
            # )
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
                    try:
                        if event.message_type == 'group':
                            self.data['data']['group_conversations'][event.group_id] = \
                                config.plugins_config['openai_chat_plugin']['presents'][present_name]['conversations']
                        else:
                            self.data['data']['user_conversations'][event.user_id] = \
                                config.plugins_config['openai_chat_plugin']['presents'][present_name]['conversations']
                    except KeyError:
                        await event.reply_text(f"预设 {present_name} 不存在。")
                        return
                    else:
                        display_name = config.plugins_config['openai_chat_plugin']['presents'][present_name].get(
                            'display_name', present_name)
                    await event.reply_text(f"已设置当前预设为: {present_name}({display_name})")
                else:  # 指定了目标
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self.data['data']['group_conversations'][group_id] = \
                                config.plugins_config['openai_chat_plugin']['presents'][present_name]['conversations']
                            display_name = config.plugins_config['openai_chat_plugin']['presents'][present_name].get(
                                'display_name', present_name)
                            await event.reply_text(f"已为群组 {group_id} 设置预设: {present_name}({display_name})")
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self.data['data']['user_conversations'][user_id] = \
                                config.plugins_config['openai_chat_plugin']['presents'][present_name]['conversations']
                            display_name = config.plugins_config['openai_chat_plugin']['presents'][present_name].get(
                                'display_name', present_name)
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

                if target is None:
                    # 重置当前会话
                    if event.message_type == 'group':
                        self.data['data']['group_conversations'][event.group_id] = \
                            config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
                    else:
                        self.data['data']['user_conversations'][event.user_id] = \
                            config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
                    await event.reply_text("已重置当前会话。")
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self.data['data']['group_conversations'][group_id] = \
                                config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
                            await event.reply_text(f"已重置群组 {group_id} 的会话。")
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self.data['data']['user_conversations'][user_id] = \
                                config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
                            await event.reply_text(f"已重置用户 {user_id} 的会话。")
                        else:
                            await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")
                    except (ValueError, IndexError):
                        await event.reply_text("目标格式错误，请使用 group:<id> 或 user:<id>。")

            # 功能：显示管理员帮助信息
            elif command[1] == 'help':
                help_text = """OpenAI Chat Plugin 管理员命令帮助：

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
                await event.reply_text(help_text)

            else:
                await event.reply_text("未知管理员命令，请使用 /chat-admin help 查看帮助信息。")

    async def user_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理用户命令事件"""
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
            await event.reply_text(
                "命令无效，请执行\n/nchelp OpenAIChatPlugin\n获得帮助信息。"
            )
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
                try:
                    self.data['data'][conversation_dict][
                        event.group_id if event.message_type == 'group' else event.user_id] = \
                        config.plugins_config['openai_chat_plugin']['presents'][present_name]['conversations']
                except KeyError:
                    await event.reply_text(f"预设 {present_name} 不存在。")
                    return
                else:
                    display_name = config.plugins_config['openai_chat_plugin']['presents'][present_name].get(
                        'display_name', present_name)
                await event.reply_text(f"已设置当前预设为: {present_name}({display_name})")

            # 功能：重置当前会话
            # 例如：/chat reset
            elif command[1] == 'reset':
                # 重置当前会话
                self.data['data'][conversation_dict][
                    event.group_id if event.message_type == 'group' else event.user_id] = \
                    config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
                await event.reply_text("已重置当前会话。")

            # 功能：显示帮助信息
            elif command[1] == 'help':
                help_text = """OpenAI Chat Plugin 用户命令帮助：

/chat set-present <name> - 设置预设（仅限当前用户/群组）
/chat reset - 重置当前会话
/chat help - 显示此帮助信息

示例：
/chat set-present MyPresent
/chat reset
/chat help
"""
                await event.reply_text(help_text)

            else:
                await event.reply_text("未知命令，请使用 /chat help 查看帮助信息。")

    async def on_load(self):
        self.register_config(
            "api_key", description="你的OpenAI API Key", allowed_values=["str"],
            default="sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
        )
        self.register_config(
            "model", description="使用的模型", allowed_values=["gpt-3.5-turbo", "gpt-4"],
            default="gpt-3.5-turbo"
        )
        self.register_config(
            "base_url", description="OpenAI API 基础URL", allowed_values=["str"],
            default="https://api.openai.com/v1"
        )
        self.register_config(
            "insert_username_as_prefix",
            description="是否在消息前添加用户名作为前缀，如\{username\}: \{conversation\}",
            allowed_values=["bool"],
            default=False
        )
        self.register_config(
            "must_at_bot", description="是否必须@机器人才能触发对话",
            allowed_values=["bool"],
            default=True
        )
        self.register_config(
            "is_configured", description="插件是否已配置",
            allowed_values=["bool"],
            default=False
        )

        self.register_user_func("用户命令", self.user_command_handler, prefix='/chat', description="设置预设、重置会话", usage="/chat <set-present|reset|help> [present_name]",
                                examples=[
                                    "/chat set-present MyPresent",  # 设置预设
                                    "/chat reset",  # 重置当前会话
                                    "/chat help"  # 显示帮助信息
                                ])

        self.register_admin_func("管理员命令", self.admin_command_handler, prefix='/chat-admin', description="跨群组/用户设置预设、重置会话", usage="/chat-admin <set-present|reset|help> [args]",
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

        # 判断是否已配置
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件。")

        # 检查是否正确添加了预设
        if not config.plugins_config['openai_chat_plugin']:
            _log.error("插件预设未正确添加，请检查预设。")

            # 设置`is_configured`为False
            self.config['is_configured'] = False

    async def _handle_message(self, event: GroupMessage | PrivateMessage | BaseMessage):
        """处理消息事件

        :param event: 事件对象
        :return: None
        """
        # 检查是否已配置插件
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件后再使用。")
            return

        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理
        if event.raw_message.strip().startswith('/'):
            _log.debug("检测到命令消息，跳过聊天处理以避免重复触发。")
            return

        client = OpenAI(
            api_key=self.config['api_key'],
            base_url=self.config['base_url']
        )

        user_message = f"{event.sender.nickname}: {event.raw_message}" if self.config[
            'insert_username_as_prefix'] else event.message

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
                self.data['data'][conversation_dict][event.group_id] = \
                    config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
            self.data['data'][conversation_dict][event.group_id].append({"role": "user", "content": user_message})
        else:
            conversation_dict = 'user_conversations'
            if event.user_id not in self.data['data'][conversation_dict]:  # 私聊消息
                self.data['data'][conversation_dict][event.user_id] = \
                    config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']
            self.data['data'][conversation_dict][event.user_id].append({"role": "user", "content": user_message})

        try:
            # 添加用户消息到会话
            response = client.chat.completions.create(
                model=self.config['model'],
                messages=self.data['data'][conversation_dict][
                    event.group_id if event.message_type == 'group' else event.user_id]
            )
            reply_message = response.choices[0].message.content

            # 回复消息
            await event.reply(reply_message)

            # 添加AI回复到会话
            self.data['data'][conversation_dict][
                event.group_id if event.message_type == 'group' else event.user_id].append(
                {"role": "assistant", "content": reply_message})
        except Exception as e:
            _log.error(f"API 调用失败: {e}")
            # await event.reply("抱歉，AI 服务暂时不可用，请稍后再试。")

    @bot.group_event()
    async def on_group_message(self, event: GroupMessage):
        """处理群消息事件"""
        await self._handle_message(event)

    @bot.private_event()
    async def on_private_message(self, event: BaseMessage):
        """处理私聊消息事件"""
        await self._handle_message(event)
