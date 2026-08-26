# -*- coding: utf-8 -*-
import json
import os
import traceback

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, MessageEvent, PrivateMessageEvent
from ncatbot.plugin import NcatBotPlugin
from ncatbot.types import At
from ncatbot.utils import get_config_manager
from ncatbot.utils.logger import get_log
from openai import OpenAI

from . import exceptions, tools
from .command_handler import (
    ADMIN_PERMISSION,
    DEFAULT_PRESENT_NAME,
    OpenAICommandHandlerMixin,
)
from .present_manager import Present
from .update import is_need_update, migrate_legacy_workspace, update_data

_log = get_log('openai_chat_plugin')  # 日志记录器

# 省略文本长度
OMITTED_TEXT_LENGTH = 100


class OpenAIChatPlugin(OpenAICommandHandlerMixin, NcatBotPlugin):
    """OpenAI 对话插件（NcatBot 5）

    通过 OpenAI API 实现智能对话，支持多预设管理、函数调用（Function Calling）、会话持久化。
    """

    name = 'OpenAIChatPlugin'  # 插件名（须与 manifest.toml 一致）
    version = '0.2.0'  # 插件版本（须与 manifest.toml 一致）
    author = 'Yang-qwq'
    description = 'OpenAI 对话插件：多预设管理、函数调用（Function Calling）、会话持久化'

    async def on_load(self):
        # ---- 注册配置默认值 ----
        # v5 ConfigMixin：仅内存补充缺失键；可被全局 config.yaml 的
        # plugin.plugin_configs.OpenAIChatPlugin 覆盖（高优先级）
        self.init_defaults({
            'ApiKey': 'sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx',  # 你的OpenAI API Key
            'Model': 'openai/gpt-4o-mini',  # 使用的模型
            'BaseUrl': 'https://api.openai.com/v1',  # OpenAI API 基础URL
            # 是否修改用户消息，在消息前添加用户名作为前缀（例如：'Alice(123456): 你好'），
            # 以便模型更好地区分不同用户的消息
            'InsertUserdataAsPrefix': False,
            'MustAtBot': True,  # 是否必须@机器人才能触发对话
            'EnableBuiltinFunctionCalling': False,  # 是否启用内置函数调用功能
            # 是否允许AI记录和访问会话历史（内置函数调用功能需要开启）
            'AllowAccessMemory': False,
            # 是否允许AI进行网络请求（内置函数调用功能需要开启）
            'AllowWebRequests': False,
            # 启用内置函数调用功能时，模型调用工具后重新生成回复的最大重试次数
            'MaxRetriesTimes': 15,
            # 最大对话缓存存储数量，用于AI获取非唤醒时错过的上下文消息，超过该数量的最早消息将被丢弃
            'MaxConversationCacheStoreAmount': 1000,
            'IsConfigured': False,  # 插件是否已配置
            # 群主/群管理自动放行本群管理员命令（/chat-admin）
            'EnableGroupOwnerAutoAuth': True,
        })

        # ---- RBAC：注册管理员权限点并自动授予 root（幂等） ----
        self.add_permission(ADMIN_PERMISSION)
        root = get_config_manager().config.root
        if root and self.rbac and not self.check_permission(root, ADMIN_PERMISSION):
            self.rbac.grant('user', root, ADMIN_PERMISSION)
            _log.info(f'已自动授予 root({root}) 插件全局管理员权限')

        # ---- 初始化持久化数据结构（逐字段补齐，兼容旧版本数据缺字段的情况） ----
        self.data.setdefault('data', {
            'group_conversations': {},
            'user_conversations': {},
            'group_preset_names': {},
            'user_preset_names': {}
        })
        for key in ('group_conversations', 'user_conversations',
                    'group_preset_names', 'user_preset_names'):
            self.data['data'].setdefault(key, {})

        # 准备历史信息查询缓存 kv（query_missing_message_context 工具使用）
        self.temp_history_messages_kv = {}

        workspace_path = str(self.workspace)

        # ---- 一次性迁移旧版 4.x 运行时数据到 v5 新位置（失败不阻塞加载） ----
        try:
            if migrate_legacy_workspace(self):
                _log.info('已完成 openai_chat_plugin 4.x 运行时数据迁移')
            else:
                _log.debug('未检测到需要迁移的 4.x 运行时数据')
        except Exception as e:  # 迁移失败不应阻塞插件整体加载
            _log.error(f'迁移旧版运行时数据失败：{e}')

        # ---- 记忆格式升级迁移（legacy memory.json -> v0.1.4+，失败不阻塞加载） ----
        try:
            if is_need_update(self):
                _log.info('检测到需要升级的记忆格式，正在迁移数据...')
                update_data(self)
            else:
                _log.debug('未检测到需要迁移的数据，跳过记忆格式升级')
        except Exception as e:
            _log.error(f'迁移记忆数据失败：{e}')

        # ---- 检查/创建默认预设 ----
        default_config_path = os.path.join(workspace_path, 'presents', DEFAULT_PRESENT_NAME, 'config.yaml')
        default_prompt_path = os.path.join(workspace_path, 'presents', DEFAULT_PRESENT_NAME, 'prompt.md')
        if os.path.exists(default_config_path) and os.path.exists(default_prompt_path):
            _log.debug('检测到默认预设已存在，跳过创建默认预设')
        else:
            # 创建默认预设目录及空 prompt.md，用户可自行编辑添加 system 提示词
            default_preset_dir = os.path.join(workspace_path, 'presents', DEFAULT_PRESENT_NAME)
            os.makedirs(default_preset_dir, exist_ok=True)

            if not os.path.exists(default_config_path):
                with open(default_config_path, 'w', encoding='utf-8') as f:
                    f.write('display_name: 默认预设\n')

            if not os.path.exists(default_prompt_path):
                with open(default_prompt_path, 'w', encoding='utf-8') as f:
                    f.write('')  # 创建一个空的 prompt.md 文件
                _log.info('已创建默认预设的 prompt.md 文件，请编辑该文件添加 system 提示词')

        # ---- 判断是否已配置 ----
        if not self.get_config('IsConfigured'):
            _log.warning(
                '插件未配置，请先在全局 config.yaml 的 plugin_configs.OpenAIChatPlugin '
                '中设置 ApiKey / Model / BaseUrl 并将 IsConfigured 置为 true')

        # 检查默认预设是否存在
        default_present = Present()
        if not default_present.load(workspace_path, DEFAULT_PRESENT_NAME):
            _log.error('默认预设不存在，请确保数据目录中存在 presents/default/ 目录及其配置文件')
            # 设置`IsConfigured`为False并持久化
            self.set_config('IsConfigured', False)

        # 创建默认OpenAI客户端
        self._default_client = OpenAI(
            api_key=self.get_config('ApiKey'),
            base_url=self.get_config('BaseUrl')
        )

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

    async def _build_user_message(self, event: MessageEvent) -> str:
        """构建用户消息，若配置了`InsertUserdataAsPrefix`，则在消息前添加用户名和用户ID作为前缀

        :param event: 消息事件
        :return: 构建后的用户消息
        """
        return f'{event.sender.nickname}({event.sender.user_id}): {event.raw_message}' if self.get_config(
            'InsertUserdataAsPrefix') else event.raw_message

    async def _handle_message(self, event: MessageEvent):
        """处理消息事件

        :param event: 事件对象
        :return: None
        :raises exceptions.TooManyToolCallsException: 当连续工具调用次数达到上限时抛出
        """
        is_group = isinstance(event, GroupMessageEvent)

        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理
        if event.raw_message.strip().startswith('/'):
            return

        # 检查是否已配置插件
        if not self.get_config('IsConfigured'):
            _log.warning('插件未配置，请先配置插件后再使用')
            return

        user_message = await self._build_user_message(event)

        if is_group:  # 群消息
            conversation_dict = 'group_conversations'

            # 检查是否必须@机器人才能触发对话
            if self.get_config('MustAtBot'):
                at_bot = any(
                    isinstance(segment, At) and segment.user_id == str(event.self_id)
                    for segment in event.message
                )
                if not at_bot:
                    _log.debug('群消息未@机器人，忽略该消息')
                    return
                _log.debug('群消息已@机器人，处理该消息')

            session_id = event.group_id
        else:  # 私聊消息
            conversation_dict = 'user_conversations'
            session_id = event.user_id

        # 检查会话是否存在，不存在则用默认预设初始化
        if session_id not in self.data['data'][conversation_dict]:
            default_present = Present()
            if not default_present.load(str(self.workspace), DEFAULT_PRESENT_NAME):
                _log.error('默认预设不存在，无法初始化会话')
                return
            self.data['data'][conversation_dict][session_id] = default_present.to_conversations()
            self._set_preset_name(conversation_dict, session_id, DEFAULT_PRESENT_NAME)

        # 添加用户消息到会话
        self.data['data'][conversation_dict][session_id].append({'role': 'user', 'content': user_message})
        _log.info(
            f'[{"群组" if is_group else "用户"} {session_id}] 用户输入: '
            f'{user_message[:OMITTED_TEXT_LENGTH]}{"..." if len(user_message) > OMITTED_TEXT_LENGTH else ""}')

        response = None
        try:
            current_retries_times = 0

            # 如果启用了内置函数调用功能，则在模型想要调用工具时会循环执行工具调用并获取结果，
            # 直到模型不再想要调用工具或达到最大重试次数为止
            while current_retries_times < self.get_config('MaxRetriesTimes'):
                response = self._default_client.chat.completions.create(
                    model=self.get_config('Model'),
                    messages=self.data['data'][conversation_dict][session_id],
                    tools=tools.tools if self.get_config('EnableBuiltinFunctionCalling') else None,
                    tool_choice='auto' if self.get_config('EnableBuiltinFunctionCalling') else 'none',
                )

                _log.debug(
                    f'请求尝试：{current_retries_times + 1}/{self.get_config("MaxRetriesTimes")}，'
                    f'模型回复: {response.choices[0].message.content}, '
                    f'finish_reason: {response.choices[0].finish_reason}, '
                    f'tool_calls: {response.choices[0].message.tool_calls}'
                )

                # 模型是否主动停止生成回复
                if response.choices[0].finish_reason == 'stop':
                    _log.debug('模型回复已完成，无需继续处理工具调用。')
                    break

                # 检查模型是否想要调用工具
                if self.get_config('EnableBuiltinFunctionCalling'):
                    if response.choices[0].message.tool_calls:
                        current_retries_times += 1
                        thinking_content = (response.choices[0].message.content or '').strip()
                        if thinking_content:
                            _log.info(
                                f'[{"群组" if is_group else "用户"} {session_id}] '
                                f'AI思考/中间内容: {thinking_content[:OMITTED_TEXT_LENGTH]}'
                                f'{"..." if len(thinking_content) > OMITTED_TEXT_LENGTH else ""}'
                            )

                        # 完整 assistant 轮次（含 tool_calls）必须先于各条 tool 消息写入历史
                        assistant_msg = response.choices[0].message
                        self.data['data'][conversation_dict][session_id].append(
                            self._assistant_message_to_history_dict(assistant_msg))

                        # 可选：将调用工具前的正文发送
                        if assistant_msg.content:
                            if is_group:
                                await self.api.qq.post_group_msg(event.group_id, text=assistant_msg.content)
                            else:
                                await self.api.qq.post_private_msg(event.user_id, text=assistant_msg.content)

                        # 处理每个工具调用请求
                        preset_name = self._get_preset_name(conversation_dict, session_id)
                        for tool_call in response.choices[0].message.tool_calls:
                            tool_name = tool_call.function.name
                            tool_args = json.loads(tool_call.function.arguments)

                            # 以下工具不需要权限，直接可调用
                            if tool_name == 'get_system_time':
                                result = await tools.get_system_time()
                            elif tool_name == 'get_environment_info':
                                result = await tools.get_environment_info(event)
                            elif tool_name == 'get_stranger_info':
                                result = await tools.get_stranger_info(self.api.qq, **tool_args)
                            elif tool_name == 'get_group_info':
                                result = await tools.get_group_info(self.api.qq, **tool_args)
                            elif tool_name == 'query_missing_message_context':
                                result = await tools.query_missing_message_context(
                                    self.temp_history_messages_kv, event.group_id)

                            # 以下工具需要配置权限才能调用
                            elif tool_name == 'access_memory':
                                if not self.get_config('AllowAccessMemory'):
                                    # 如果不允许访问记忆功能，则拒绝工具调用请求并返回错误信息
                                    result = tools._generate_tool_payload(
                                        'error', '`AllowAccessMemory` 配置未启用，无法使用记忆功能')
                                    _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许访问记忆功能')
                                else:
                                    # 来源由会话决定
                                    tool_args['from_user'] = event.user_id
                                    tool_args['from_group'] = event.group_id if is_group else -1
                                    result = tools.access_memory(
                                        os.path.join(str(self.workspace), 'presents', preset_name), **tool_args
                                    )
                            else:
                                _log.warning(f'未知工具调用请求: {tool_name}')
                                result = tools._generate_tool_payload('error', f'未知工具: {tool_name}')

                            _log.info(
                                f'[{"群组" if is_group else "用户"} {session_id}] 工具调用: '
                                f'{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:OMITTED_TEXT_LENGTH]}) -> '
                                f'{str(result)[:OMITTED_TEXT_LENGTH]}'
                                f'{"..." if len(str(result)) > OMITTED_TEXT_LENGTH else ""}'
                            )

                            # 将工具调用结果添加到会话中，供模型后续生成回复时参考
                            self.data['data'][conversation_dict][session_id].append(
                                {'tool_call_id': tool_call.id, 'role': 'tool', 'name': tool_name, 'content': result})
                    else:
                        break

            # MaxRetriesTimes 配置 <= 0 时循环不会执行，避免后续解引用未定义的 response
            if response is None:
                _log.error('MaxRetriesTimes 配置须为正整数，已跳过本次对话请求')
                return

            last_msg = response.choices[0].message
            reply_message = last_msg.content or ''
            # 最后一轮 API 仍在请求工具时 while 已无法继续，content 往往为空，避免 reply(None)
            if last_msg.tool_calls and not reply_message.strip():
                raise exceptions.TooManyToolCallsException('抱歉，连续工具调用次数已达上限')

            _log.info(
                f'[{"群组" if is_group else "用户"} {session_id}] AI回复: '
                f'{reply_message[:OMITTED_TEXT_LENGTH]}{"..." if len(reply_message) > OMITTED_TEXT_LENGTH else ""}'
            )

            # 回复消息（保持旧版不 @ 发送者的行为）
            await event.reply(reply_message, at_sender=False)

            # 添加AI回复到会话
            self.data['data'][conversation_dict][session_id].append({'role': 'assistant', 'content': reply_message})

            # 保存持久化文件
            self._save_data()
        except exceptions.TooManyToolCallsException as e:
            await event.reply(text=str(e), at_sender=False)

        except Exception:
            _log.error(traceback.format_exc())
            await event.reply(text='抱歉，插件出现内部错误，请稍后再试', at_sender=False)

    @registrar.qq.on_group_message()
    async def on_group_message(self, event: GroupMessageEvent):
        """处理群消息事件：跳过命令 → 记录历史 → 处理对话

        注意：4.x 版本曾存在两个同名方法相互覆盖、群聊对话失效的问题，
        v5 迁移时已合并为单一入口。

        :param event: 群消息事件
        :return: None
        """
        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理与历史记录
        if event.raw_message.strip().startswith('/'):
            return

        # 记录消息历史（供 query_missing_message_context 工具查询）
        await self._record_group_history(event)

        await self._handle_message(event)

    async def _record_group_history(self, event: GroupMessageEvent):
        """记录群消息到临时历史缓存（仅 EnableBuiltinFunctionCalling 开启时）

        缓存长度受 MaxConversationCacheStoreAmount 限制，超过则丢弃最早的消息。

        :param event: 群消息事件
        :return: None
        """
        if not self.get_config('EnableBuiltinFunctionCalling'):
            return

        group_key = str(event.group_id)
        # 如果不存在群记录，则新建该key
        self.temp_history_messages_kv.setdefault(group_key, []).append(await self._build_user_message(event))
        _log.debug(
            f'[群组 {group_key}:{event.user_id}] 记录消息：'
            f'{event.raw_message[:100]}{"..." if len(event.raw_message) > 100 else ""}')

        # 长度超过限制时删除最早的消息
        limit = self.get_config('MaxConversationCacheStoreAmount')
        while len(self.temp_history_messages_kv[group_key]) > limit:
            self.temp_history_messages_kv[group_key].pop(0)
            _log.debug(f'[群组 {group_key}] 超过最大缓存存储数量，已删除最早的消息记录')

    @registrar.qq.on_private_message()
    async def on_private_message(self, event: PrivateMessageEvent):
        """处理私聊消息事件

        :param event: 私聊消息事件
        :return: None
        """
        # 检查消息是否以命令前缀开头，如果是则跳过聊天处理
        if event.raw_message.strip().startswith('/'):
            return

        await self._handle_message(event)
