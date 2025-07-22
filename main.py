# -*- coding: utf-8 -*-
from ncatbot.core import GroupMessage, BaseMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log("openai_chat_plugin")  # 日志记录器


class OpenAIChatPlugin(BasePlugin):
    name = "OpenAIChatPlugin"  # 插件名
    version = "0.0.1"  # 插件版本

    # async def command_handler(self, base_message: BaseMessage):
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

            # 设置`is_configured`为False\
            self.config['is_configured'] = False

    @bot.group_event()
    async def on_group_message(self, event: GroupMessage):
        """处理群消息事件"""
        # 检查是否已配置插件
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件后再使用。")
            return
        else:
            # 配置OpenAI客户端
            client = OpenAI(
                api_key=self.config['api_key'],
                base_url=self.config['base_url']
            )

            # 检查是否已存在群会话
            if event.group_id not in self.data['data']['group_conversations']:
                # 如果不存在，则创建一个新的会话
                # 从配置读取system prompt内容
                _log.debug("正在为群%s创建新的会话...", event.group_id)
                self.data['data']['group_conversations'][event.group_id] = \
                config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']  # 默认会话内容

            # 检查是否需要@机器人
            if self.config['must_at_bot']:
                at_bot = False
                for msg in event.message:
                    if 'at' in msg['type'] and msg['data']['qq'] == config.bt_uin:
                        _log.debug("消息已@机器人，开始处理。")
                        at_bot = True
                        break
                if not at_bot:
                    _log.debug("消息未@机器人，忽略处理。")
                    return

            # 添加用户消息到会话
            if self.config['insert_username_as_prefix']:
                user_message = f"{event.sender.nickname}: {event.raw_message}"
            else:
                user_message = event.message

            # 将用户消息添加到会话中
            self.data['data']['group_conversations'][event.group_id].append({"role": "user", "content": user_message})
            _log.debug(f"收到需处理的消息：{user_message}")

            # 调用OpenAI API获取回复
            response = client.chat.completions.create(
                model=self.config['model'],
                messages=self.data['data']['group_conversations'][event.group_id]
            )
            reply_message = response.choices[0].message.content
            _log.debug(f"API应答：{reply_message}")

            # 发送回复
            await event.reply(reply_message)

            # 将回复添加到会话中
            self.data['data']['group_conversations'][event.group_id].append({"role": "assistant", "content": reply_message})
