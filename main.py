# -*- coding: utf-8 -*-
import json
import os
import traceback

from ncatbot.core import BaseMessage, GroupMessage, PrivateMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI

from . import exceptions, tools
from .command_handler import DEFAULT_PRESENT_NAME, OpenAICommandHandlerMixin
from .present_manager import Present
from .update import is_need_update, update_data

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log('openai_chat_plugin')  # 日志记录器

# 省略文本长度
OMITTED_TEXT_LENGTH = 100

class OpenAIChatPlugin(OpenAICommandHandlerMixin, BasePlugin):
    name = 'OpenAIChatPlugin'  # 插件名
    version = '0.1.9'  # 插件版本

    async def on_load(self):
        self.register_config(
            'ApiKey', description='你的OpenAI API Key', default='sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',
            value_type='str'
        )
        self.register_config(
            'Model', description='使用的模型', default='openai/gpt-4o-mini', value_type='str'
        )
        self.register_config(
            'BaseUrl', description='OpenAI API 基础URL', default='https://api.openai.com/v1', value_type='str'
        )
        self.register_config(
            'InsertUserdataAsPrefix',
            description="是否修改用户消息，在消息前添加用户名作为前缀（例如：'Alice(123456): 你好'），以便模型更好地区分不同用户的消息",
            default=False
        )
        self.register_config(
            'MustAtBot', description='是否必须@机器人才能触发对话',
            value_type='bool',
            default=True
        )
        self.register_config(
            'EnableBuiltinFunctionCalling', description='是否启用内置函数调用功能', value_type='bool',
            default=False
        )
        self.register_config(
            'AllowAccessMemory', description='是否允许AI记录和访问会话历史（内置函数调用功能需要开启）',
            default=False, value_type='bool'
        )
        self.register_config(
            'AllowWebRequests', description='是否允许AI进行网络请求（内置函数调用功能需要开启）',
            default=False, value_type='bool'
        )
        self.register_config('MaxRetriesTimes',
                             description='当启用内置函数调用功能时，模型想要调用工具后重新生成回复的最大重试次数',
                             value_type='int', default=15)
        self.register_config(
            'IsConfigured', description='插件是否已配置',
            value_type='bool',
            default=False
        )

        self.register_user_func('用户命令', self.user_command_handler, prefix='/chat', description='设置预设、重置会话',
                                usage='/chat <set-present|reset|help> [present_name]',
                                examples=[
                                    '/chat set-present MyPresent',  # 设置预设
                                    '/chat reset',  # 重置当前会话
                                    '/chat help'  # 显示帮助信息
                                ])

        self.register_admin_func('管理员命令', self.admin_command_handler, prefix='/chat-admin',
                                 description='跨群组/用户设置预设、重置会话',
                                 usage='/chat-admin <set-present|reset|help> [args]',
                                 examples=[
                                     '/chat-admin set-present MyPresent',  # 设置预设
                                     '/chat-admin set-present MyPresent group:1919810',  # 跨群组设置预设
                                     '/chat-admin set-present MyPresent user:114514',  # 跨用户设置预设
                                     '/chat-admin reset',  # 重置当前会话
                                     '/chat-admin reset group:1919810',  # 跨群组重置会话
                                     '/chat-admin reset user:114514',  # 跨用户重置会话
                                     '/chat-admin help'  # 显示帮助信息
                                 ])

        # 初始化持久化数据
        if 'data' not in self.data:
            self.data['data'] = {
                'group_conversations': {},
                'user_conversations': {},
                'group_preset_names': {},
                'user_preset_names': {}
            }

        # 确认每个字段都存在，避免旧版本数据缺少字段导致的KeyError
        if 'group_conversations' not in self.data['data']:
            self.data['data']['group_conversations'] = {}
        if 'user_conversations' not in self.data['data']:
            self.data['data']['user_conversations'] = {}
        if 'group_preset_names' not in self.data['data']:
            self.data['data']['group_preset_names'] = {}
        if 'user_preset_names' not in self.data['data']:
            self.data['data']['user_preset_names'] = {}

        # 检查是否已存在默认预设
        if os.path.exists(self.work_space.path.as_posix() + '/presents/default/config.yaml') and os.path.exists(
                self.work_space.path.as_posix() + '/presents/default/prompt.md'):
            _log.debug('检测到默认预设已存在，跳过创建默认预设')
        else:
            # 创建默认预设
            # 之后可能会改
            default_preset_dir = os.path.join(self.work_space.path.as_posix(), 'presents', DEFAULT_PRESENT_NAME)
            os.makedirs(default_preset_dir, exist_ok=True)

            default_config_path = os.path.join(default_preset_dir, 'config.yaml')
            default_prompt_path = os.path.join(default_preset_dir, 'prompt.md')

            if not os.path.exists(default_config_path):
                with open(default_config_path, 'w', encoding='utf-8') as f:
                    f.write('display_name: 默认预设\n')

            # 仅创建`prompt.md`文件，不写入任何内容，用户可以自行编辑添加 system 提示词
            if not os.path.exists(default_prompt_path):
                with open(default_prompt_path, 'w', encoding='utf-8') as f:
                    f.write('')  # 创建一个空的 prompt.md 文件
                _log.info('已创建默认预设的 prompt.md 文件，请编辑该文件添加 system 提示词')

        # 判断是否已配置
        if not self.config['IsConfigured']:
            _log.warning('插件未配置，请先配置插件')

        # 在插件加载时尝试迁移旧版预设配置到数据目录
        try:
            if is_need_update(self):
                _log.info('检测到需要迁移的预设配置，正在迁移数据...')
                update_data(self)
            else:
                _log.debug('未检测到需要迁移的预设配置，跳过数据迁移')
        except Exception as e:  # 迁移失败不应阻塞插件整体加载
            _log.error(f'迁移预设数据失败：{e}')

        # 检查默认预设是否存在
        default_present = Present()
        if not default_present.load(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME):
            _log.error('默认预设不存在，请确保数据目录中存在 presents/default/ 目录及其配置文件')
            # 设置`IsConfigured`为False
            self.config['IsConfigured'] = False

        # 创建默认OpenAI客户端
        self._default_client = OpenAI(
            api_key=self.config['ApiKey'],
            base_url=self.config['BaseUrl']
        )

        # 准备历史信息查记录kv
        if self.config['EnableBuiltinFunctionCalling']:
            self.temp_history_messages_kv = {}

    def _assistant_message_to_history_dict(self, assistant_message) -> dict:
        """将 API 返回的 assistant 消息转为可写入 messages 历史的 dict（含 tool_calls）。

        :param assistant_message: API 返回的 assistant 消息
        :return: dict, 可写入 messages 历史的 dict（含 tool_calls）
        """
        entry: dict = {'role': 'assistant', 'content': assistant_message.content}
        tcs = assistant_message.tool_calls
        if tcs:
            entry['tool_calls'] = [
                {
                    'id': tc.id,
                    'type': getattr(tc, 'type', None) or 'function',
                    'function': {
                        'name': tc.function.name,
                        'arguments': tc.function.arguments or '{}',
                    },
                }
                for tc in tcs
            ]
        return entry

    async def _handle_message(self, event: GroupMessage | PrivateMessage | BaseMessage):
        """处理消息事件

        :param event: 事件对象
        :return: None
        :raises exceptions.TooManyToolCallsException: 当连续工具调用次数达到上限时抛出
        """

        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理
        if event.raw_message.strip().startswith('/'):
            return

        # 检查是否已配置插件
        if not self.config['IsConfigured']:
            _log.warning('插件未配置，请先配置插件后再使用')
            return

        user_message = f'{event.sender.nickname}({event.sender.user_id}): {event.raw_message}' if self.config[
            'InsertUserdataAsPrefix'] else event.raw_message

        if event.message_type == 'group':  # 群消息
            conversation_dict = 'group_conversations'

            # 检查是否必须@机器人才能触发对话
            if self.config['MustAtBot']:
                _at_bot = False
                for msg in event.message:
                    if 'at' in msg['type'] and msg['data']['qq'] == config.bt_uin:
                        _log.debug('群消息已@机器人，处理该消息')
                        _at_bot = True
                        break  # 找到@机器人消息后退出循环
                if not _at_bot:
                    _log.debug('群消息未@机器人，忽略该消息')
                    return

            # 检查群会话是否存在
            if event.group_id not in self.data['data'][conversation_dict]:
                default_present = Present()
                if not default_present.load(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME):
                    _log.error('默认预设不存在，无法初始化会话')
                    return
                self.data['data'][conversation_dict][event.group_id] = default_present.to_conversations()
                self._set_preset_name(conversation_dict, event.group_id, DEFAULT_PRESENT_NAME)

            self.data['data'][conversation_dict][event.group_id].append({'role': 'user', 'content': user_message})
            _log.info(
                f'[群组 {event.group_id}] 用户输入: {user_message[:OMITTED_TEXT_LENGTH]}{"..." if len(user_message) > 200 else ""}')
        else:
            conversation_dict = 'user_conversations'
            if event.user_id not in self.data['data'][conversation_dict]:  # 私聊消息
                default_present = Present()
                if not default_present.load(self.work_space.path.as_posix() + '/', DEFAULT_PRESENT_NAME):
                    _log.error('默认预设不存在，无法初始化会话')
                    return
                self.data['data'][conversation_dict][event.user_id] = default_present.to_conversations()
                self._set_preset_name(conversation_dict, event.user_id, DEFAULT_PRESENT_NAME)

            # 添加用户消息到会话
            self.data['data'][conversation_dict][event.user_id].append({'role': 'user', 'content': user_message})
            _log.info(
                f'[用户 {event.user_id}] 用户输入: {user_message[:OMITTED_TEXT_LENGTH]}{"..." if len(user_message) > OMITTED_TEXT_LENGTH else ""}')

        try:
            current_retries_times = 0

            # 如果启用了内置函数调用功能，则在模型想要调用工具时会循环执行工具调用并获取结果，直到模型不再想要调用工具或达到最大重试次数为止
            while current_retries_times < self.config['MaxRetriesTimes']:
                response = self._default_client.chat.completions.create(
                    model=self.config['Model'],
                    messages=self.data['data'][conversation_dict][
                        event.group_id if event.message_type == 'group' else event.user_id],
                    tools=tools.tools if self.config['EnableBuiltinFunctionCalling'] else None,
                    tool_choice='auto' if self.config['EnableBuiltinFunctionCalling'] else 'none',
                )

                _log.debug(
                    f'请求尝试：{current_retries_times + 1}/{self.config["MaxRetriesTimes"]}，'
                    f'模型回复: {response.choices[0].message.content}, '
                    f'finish_reason: {response.choices[0].finish_reason}, '
                    f'tool_calls: {response.choices[0].message.tool_calls}'
                )

                # 模型是否主动停止生成回复
                if response.choices[0].finish_reason == 'stop':
                    _log.debug('模型回复已完成，无需继续处理工具调用。')
                    break

                # 检查模型是否想要调用工具
                if self.config['EnableBuiltinFunctionCalling']:
                    if response.choices[0].message.tool_calls:
                        current_retries_times += 1
                        thinking_content = (response.choices[0].message.content or '').strip()
                        if thinking_content:
                            _session_id = event.group_id if event.message_type == 'group' else event.user_id
                            _log.info(
                                f'[{"群组" if event.message_type == "group" else "用户"} {_session_id}] '
                                f'AI思考/中间内容: {thinking_content[:OMITTED_TEXT_LENGTH]}{"..." if len(thinking_content) > OMITTED_TEXT_LENGTH else ""}'
                            )

                        # 完整 assistant 轮次（含 tool_calls）必须先于各条 tool 消息写入历史
                        assistant_msg = response.choices[0].message
                        self.data['data'][conversation_dict][
                            event.group_id if event.message_type == 'group' else event.user_id
                        ].append(self._assistant_message_to_history_dict(assistant_msg))

                        # 可选：将调用工具前的正文发送
                        if assistant_msg.content:
                            if event.message_type == 'group':
                                await self.api.post_group_msg(event.group_id, assistant_msg.content)
                            else:
                                await self.api.post_private_msg(event.user_id, assistant_msg.content)

                        # 处理每个工具调用请求
                        session_id = event.group_id if event.message_type == 'group' else event.user_id
                        preset_name = self._get_preset_name(conversation_dict, session_id)
                        for tool_call in response.choices[0].message.tool_calls:
                            tool_name = tool_call.function.name
                            tool_args = json.loads(tool_call.function.arguments)

                            # 以下工具不需要权限，直接可调用
                            if tool_name == 'get_system_time':
                                result = tools.get_system_time()
                            elif tool_name == 'get_environment_info':
                                result = tools.get_environment_info(event)
                            elif tool_name == 'get_stranger_info':
                                result = await tools.get_stranger_info(self.api, **tool_args)
                            elif tool_name == 'get_group_info':
                                result = await tools.get_group_info(self.api, **tool_args)

                            # 以下工具需要配置权限才能调用
                            elif tool_name == 'access_memory':
                                if not self.config['AllowAccessMemory']:  # 如果不允许访问记忆功能，则拒绝工具调用请求并返回错误信息
                                    result = tools._generate_tool_payload(
                                        'error', '`AllowAccessMemory` 配置未启用，无法使用记忆功能')
                                    _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许访问记忆功能')
                                else:
                                    # 来源由会话决定
                                    tool_args['from_user'] = event.user_id
                                    tool_args['from_group'] = (
                                        event.group_id if event.message_type == 'group' else -1
                                    )
                                    result = tools.access_memory(
                                        os.path.join(
                                            self.work_space.path.as_posix(), 'presents', preset_name
                                        ), **tool_args
                                    )
                            else:
                                _log.warning(f'未知工具调用请求: {tool_name}')
                                result = tools._generate_tool_payload('error', f'未知工具: {tool_name}')

                            _log.info(
                                f'[{"群组" if event.message_type == "group" else "用户"} {session_id}] 工具调用: '
                                f'{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:OMITTED_TEXT_LENGTH]}) -> '
                                f'{str(result)[:OMITTED_TEXT_LENGTH]}{"..." if len(str(result)) > OMITTED_TEXT_LENGTH else ""}'
                            )

                            # 将工具调用结果添加到会话中，供模型后续生成回复时参考
                            self.data['data'][conversation_dict][
                                event.group_id if event.message_type == 'group' else event.user_id].append(
                                {'tool_call_id': tool_call.id, 'role': 'tool', 'name': tool_name, 'content': result})
                    else:
                        break

            last_msg = response.choices[0].message
            reply_message = last_msg.content or ''
            # 最后一轮 API 仍在请求工具时 while 已无法继续，content 往往为空，避免 reply(None)
            if last_msg.tool_calls and not reply_message.strip():
                raise exceptions.TooManyToolCallsException('抱歉，连续工具调用次数已达上限')

            session_id = event.group_id if event.message_type == 'group' else event.user_id
            _log.info(
                f'[{"群组" if event.message_type == "group" else "用户"} {session_id}] AI回复: '
                f'{reply_message[:OMITTED_TEXT_LENGTH]}{"..." if len(reply_message) > OMITTED_TEXT_LENGTH else ""}'
            )

            # 回复消息
            await event.reply(reply_message)

            # 添加AI回复到会话
            self.data['data'][conversation_dict][
                event.group_id if event.message_type == 'group' else event.user_id].append(
                {'role': 'assistant', 'content': reply_message})

            # 保存持久化文件
            self.data.save()
        except exceptions.TooManyToolCallsException as e:
            await event.reply(e.__str__())

        except Exception as e:
            _log.error(f'API 调用失败: {e.__class__.__name__}: {e}')
            _log.error(traceback.format_exc())
            await event.reply('抱歉，插件出现内部错误，请稍后再试')

            # 当debug开启时向用户输出错误
            # if config.debug:
            #     await event.reply(traceback.format_exc())

    @bot.group_event()
    async def on_group_message(self, event: GroupMessage):
        """处理群消息事件

        :param event:
        :return:
        """
        await self._handle_message(event)

    # @bot.group_event()
    # async def on_group_message(self, event: GroupMessage):
    #     """记录
    #
    #     :param event:
    #     :return:
    #     """
    #     if self.config['EnableBuiltinFunctionCalling']:
    #         # 记录消息历史
    #         if not str(event.group_id) in self.temp_history_messages_kv:
    #             # 如果不存在群记录，则新建该key
    #             self.temp_history_messages_kv[str(event.group_id)] = []
    #         self.temp_history_messages_kv[str(event.group_id)].append(event)
    #
    #         # 长度是否超过限制，超过则删除最早的消息
    #         if len(self.temp_history_messages_kv[str(event.group_id)]) > 1000:
    #             self.temp_history_messages_kv[str(event.group_id)].pop(0)

    @bot.private_event()
    async def on_private_message(self, event: BaseMessage):
        """处理私聊消息事件"""
        await self._handle_message(event)
