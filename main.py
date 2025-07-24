# -*- coding: utf-8 -*-
from ncatbot.core import GroupMessage, BaseMessage, PrivateMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log("openai_chat_plugin")  # 日志记录器


class OpenAIChatPlugin(BasePlugin):
    name = "OpenAIChatPlugin"  # 插件名
    version = "0.0.2"  # 插件版本

    # async def command_handler(self, event: BaseMessage):
    #     """处理命令事件"""
    #     pass

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

        # self.register_user_func("命令事件处理器", self.command_handler, prefix='/openai', description="Test")

        # 初始化持久化数据
        if 'data' not in self.data:
            self.data['data'] = {
                'group_conversations': {},
                'user_conversations': {}
            }

        # 判断是否已配置
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件。")

        # 检查是否正确添加了配置文件
        if not config.plugins_config['openai_chat_plugin']:
            _log.error("插件配置文件未正确添加，请检查配置文件。")

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
            self.data['data'][conversation_dict][event.group_id if event.message_type == 'group' else event.user_id].append(
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
