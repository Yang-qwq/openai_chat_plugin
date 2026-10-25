# -*- coding: utf-8 -*-
import json
import os
import random
import re
import time
import traceback

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, MessageEvent, PrivateMessageEvent
from ncatbot.plugin import NcatBotPlugin
from ncatbot.types import At
from ncatbot.utils import get_config_manager
from ncatbot.utils.logger import get_log
from openai import OpenAI

from . import exceptions, reasoning, tools
from .command_handler import (
    ADMIN_PERMISSION,
    DEFAULT_PRESET_NAME,
    OpenAICommandHandlerMixin,
)
from .preset_manager import Preset
from .update import (
    migrate_legacy_workspace,
    migrate_memory_format,
    migrate_presents_dir,
    needs_memory_migration,
)

_log = get_log('openai_chat_plugin')  # 日志记录器

# 省略文本长度
OMITTED_TEXT_LENGTH = 100

# 随机插话时临时插入的提示词（不写入会话历史）：让模型自行判断是否值得插话
RANDOM_REPLY_SYSTEM_PROMPT = (
    '现在你处于群聊随机插话模式：这条消息并没有 @ 你，是否参与完全由你决定。\n'
    '只有当内容与你的设定相关、且你确实有值得补充的内容时才简短发言；'
    '如果只是日常闲聊、你没什么可说的，或插话会显得突兀，请只输出 NO_REPLY（不要输出任何其他内容）。\n'
    '不要复述本条提示，也不要解释你的判断。'
)
# 随机插话的「不发言」哨兵：容忍 NO_REPLY / NO-REPLY / `no_reply` 等写法
NO_REPLY_PATTERN = re.compile(r'^\W*no[_\-\s]?reply\W*$', re.IGNORECASE)
# 同一群的随机插话最小间隔（秒），防止连续刷屏
RANDOM_REPLY_COOLDOWN_SECONDS = 30


class OpenAIChatPlugin(OpenAICommandHandlerMixin, NcatBotPlugin):
    """OpenAI 对话插件（NcatBot 5）

    通过 OpenAI API 实现智能对话，支持多预设管理、函数调用（Function Calling）、会话持久化。
    """

    name = 'OpenAIChatPlugin'  # 插件名（须与 manifest.toml 一致）
    version = '0.4.0'  # 插件版本（须与 manifest.toml 一致）
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
            # 是否允许AI创建/管理定时任务（内置函数调用功能需要开启）
            'AllowScheduledTasks': False,
            # 是否识别并下载用户消息中携带的图片，作为视觉输入交给模型（无需开启函数调用）
            'EnableImageRecognition': True,
            # 是否剥离推理模型的思维链（<think>/<reasoning> 等标签），防止思维链暴露给用户或污染历史
            'StripReasoning': True,
            # 启用内置函数调用功能时，模型调用工具后重新生成回复的最大重试次数
            'MaxRetriesTimes': 15,
            # 最大对话缓存存储数量，用于AI获取非唤醒时错过的上下文消息，超过该数量的最早消息将被丢弃
            'MaxConversationCacheStoreAmount': 1000,
            'IsConfigured': False,  # 插件是否已配置
            # 群主/群管理自动放行本群管理员命令（/chat-admin）
            'EnableGroupOwnerAutoAuth': True,
            'EnableRandomReplyGroup': False,  # 是否随机回复群消息
            'GroupRandomReplyChance': 0.02,  # 群随机回复概率（0~1）
            'ReplaceSystemAsUser': False,  # 在部分AI提供商中只允许出现单个 system 提示词（如智谱 GLM），启用后将 system 提示词替换为 user 消息段
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
            'user_preset_names': {},
            # 群级随机插话开关：group_id -> bool（True = 该群不再随机插话）
            'group_nolisten': {},
            # AI 定时任务记录：task_name -> {interval, prompt, session_type, session_id, created_at}
            'scheduled_tasks': {}
        })
        for key in ('group_conversations', 'user_conversations',
                    'group_preset_names', 'user_preset_names', 'group_nolisten',
                    'scheduled_tasks'):
            self.data['data'].setdefault(key, {})

        # 准备历史信息查询缓存 kv（query_missing_message_context 工具使用）
        self.temp_history_messages_kv = {}
        # 随机插话冷却时间记录（内存态，不持久化）：group_id -> monotonic 时间戳
        self._last_random_reply_ts: dict[str, float] = {}

        workspace_path = str(self.workspace)

        # ---- 修正旧版工作区预设目录拼写 presents/ -> presets/（失败不阻塞加载） ----
        try:
            if migrate_presents_dir(self):
                _log.info('已将预设目录 presents/ 重命名为 presets/')
            else:
                _log.debug('预设目录无需重命名')
        except Exception as e:
            _log.error(f'重命名预设目录失败: {e}')

        # ---- 一次性迁移旧版 4.x 运行时数据到 v5 新位置（失败不阻塞加载） ----
        try:
            if migrate_legacy_workspace(self):
                _log.info('已完成 openai_chat_plugin 4.x 运行时数据迁移')
            else:
                _log.debug('未检测到需要迁移的 4.x 运行时数据')
        except Exception as e:  # 迁移失败不应阻塞插件整体加载
            _log.error(f'迁移旧版运行时数据失败: {e}')

        # ---- 记忆格式升级迁移（legacy memory.json -> v0.1.4+，失败不阻塞加载） ----
        try:
            if needs_memory_migration(self):
                _log.info('检测到需要升级的记忆格式，正在迁移数据...')
                migrate_memory_format(self)
            else:
                _log.debug('未检测到需要迁移的数据，跳过记忆格式升级')
        except Exception as e:
            _log.error(f'迁移记忆数据失败: {e}')

        # ---- 检查/创建默认预设 ----
        default_config_path = os.path.join(workspace_path, 'presets', DEFAULT_PRESET_NAME, 'config.yaml')
        default_prompt_path = os.path.join(workspace_path, 'presets', DEFAULT_PRESET_NAME, 'prompt.md')
        if os.path.exists(default_config_path) and os.path.exists(default_prompt_path):
            _log.debug('检测到默认预设已存在，跳过创建默认预设')
        else:
            # 创建默认预设目录及空 prompt.md，用户可自行编辑添加 system 提示词
            default_preset_dir = os.path.join(workspace_path, 'presets', DEFAULT_PRESET_NAME)
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
        default_preset = Preset()
        if not default_preset.load(workspace_path, DEFAULT_PRESET_NAME):
            _log.error('默认预设不存在，请确保数据目录中存在 presets/default/ 目录及其配置文件')
            # 设置`IsConfigured`为False并持久化
            self.set_config('IsConfigured', False)

        # 创建默认OpenAI客户端
        self._default_client = OpenAI(
            api_key=self.get_config('ApiKey'),
            base_url=self.get_config('BaseUrl')
        )

        # ---- 恢复持久化的 AI 定时任务（失败不阻塞加载） ----
        self._restore_scheduled_tasks()

    def _restore_scheduled_tasks(self) -> None:
        """把 data.json 中记录的定时任务重新注册到框架调度器。

        任务记录不会因为 ``AllowScheduledTasks`` 关闭而删除，该开关只控制模型能否
        新增/管理任务；注册失败时保留记录并记录警告，便于排查 interval 等问题。
        """
        tasks = self.data['data'].get('scheduled_tasks') or {}
        if not tasks:
            return
        restored = 0
        for task_name, info in tasks.items():
            if not isinstance(info, dict):
                continue
            ok = tools.register_scheduled_task(
                self,
                task_name,
                info.get('interval', ''),
                info.get('prompt', ''),
                info.get('session_type', 'group'),
                str(info.get('session_id', '')),
            )
            if ok:
                restored += 1
            else:
                _log.warning(f'定时任务 {task_name} 恢复失败（interval 非法或定时任务服务不可用）')
        if restored:
            _log.info(f'已恢复 {restored}/{len(tasks)} 个 AI 定时任务')

    def _build_task_messages(self, session_type: str, session_id: str, prompt: str) -> list[dict]:
        """构造定时任务的请求消息：会话当前预设的 system + 任务提示。

        :param session_type: 'group' 或 'private'
        :param session_id: 会话 ID
        :param prompt: 任务提示
        :return: OpenAI 消息列表
        """
        conversation_dict = 'group_conversations' if session_type == 'group' else 'user_conversations'
        try:
            preset_name = self._get_preset_name(conversation_dict, str(session_id))
        except Exception:
            preset_name = DEFAULT_PRESET_NAME

        preset = Preset()
        if not preset.load(str(self.workspace), preset_name):
            preset = Preset()
            if not preset.load(str(self.workspace), DEFAULT_PRESET_NAME):
                _log.warning('定时任务找不到可用预设，将以无 system 提示的方式执行')
                return [{'role': 'user', 'content': prompt}]
        return list(preset.to_conversations()) + [{'role': 'user', 'content': prompt}]

    def _clean_content(self, content: str | None) -> str:
        """按配置剥离思维链并去除首尾空白。

        :param content: 模型返回的原始正文
        :return: 可直接发送/写入历史的正文
        """
        cleaned = (content or '').strip()
        if self.get_config('StripReasoning'):
            cleaned = reasoning.strip_reasoning(cleaned)
        return cleaned

    def _log_reasoning(self, message, content: str) -> None:
        """提取并打印思维链（仅写日志，不发送给用户）。

        :param message: API 返回的 assistant 消息（可能含 reasoning_content 字段）
        :param content: 该消息的正文内容
        """
        reasoning_text = reasoning.extract_reasoning(content)
        field_reasoning = getattr(message, 'reasoning_content', None)
        if field_reasoning:
            field_reasoning = str(field_reasoning).strip()
            reasoning_text = (f'{reasoning_text}\n{field_reasoning}'
                              if reasoning_text else field_reasoning)
        if reasoning_text:
            _log.info(
                f'AI思维链: {reasoning_text[:OMITTED_TEXT_LENGTH]}'
                f'{"..." if len(reasoning_text) > OMITTED_TEXT_LENGTH else ""}'
            )
        if not content and not getattr(message, 'tool_calls', None):
            if reasoning_text:
                _log.warning('模型仅返回思维链而无正文，已忽略以避免暴露思维链')
            else:
                _log.warning('模型未返回任何正文内容')

    def _assistant_message_to_history_dict(self, assistant_message) -> dict:
        """将 API 返回的 assistant 消息转为可写入 messages 历史的 dict（含 tool_calls）。

        :param assistant_message: API 返回的 assistant 消息
        :return: dict, 可写入 messages 历史的 dict（含 tool_calls）
        """
        content = assistant_message.content
        # 部分模型会在正文首尾输出换行/空格，统一去除，避免历史与回复出现多余空白；
        # 同时剥离思维链，避免把 <think>…</think> 之类的推理过程写入会话历史
        if isinstance(content, str):
            content = self._clean_content(content) or None
        entry: dict = {'role': 'assistant', 'content': content}
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

    # ------------------------------------------------------------------
    # 随机插话（群聊非 @ 消息按概率参与）
    # ------------------------------------------------------------------

    def _should_random_reply(self, event: GroupMessageEvent) -> bool:
        """判断本条群消息是否触发随机插话。

        依次检查：功能开关 → 群级 nolisten → 概率 → 消息有效性 → 群冷却。

        :param event: 群消息事件
        :return: 是否随机插话
        """
        if not self.get_config('EnableRandomReplyGroup'):
            return False

        group_id = str(event.group_id)
        if self.data['data'].get('group_nolisten', {}).get(group_id, False):
            return False

        try:
            chance = float(self.get_config('GroupRandomReplyChance'))
        except (TypeError, ValueError):
            _log.warning('GroupRandomReplyChance 不是有效数字，已跳过随机插话')
            return False
        if chance <= 0:
            return False
        chance = min(chance, 1.0)

        # 纯 @ / 空消息不参与（笔记：text 只含纯文本段）
        if not event.message.text.strip() and not event.message.filter_image():
            return False

        now = time.monotonic()
        if now - self._last_random_reply_ts.get(group_id, 0.0) < RANDOM_REPLY_COOLDOWN_SECONDS:
            return False

        if random.random() >= chance:
            return False

        self._last_random_reply_ts[group_id] = now
        return True

    def _build_request_messages(self, conversation: list[dict],
                                random_reply: bool = False) -> list[dict]:
        """构造本次 API 请求的消息列表（副本，不写回会话历史）。

        随机插话时插入一条临时提示；``ReplaceSystemAsUser`` 开启时该提示改用
        user 角色，以兼容只接受单个 system 的供应商（如智谱 GLM）。

        :param conversation: 会话消息列表
        :param random_reply: 本次是否为随机插话
        :return: 供 API 使用的消息列表副本
        """
        messages = list(conversation)
        if not random_reply:
            return messages
        hint_role = 'user' if self.get_config('ReplaceSystemAsUser') else 'system'
        insert_at = 1 if messages and messages[0].get('role') == 'system' else 0
        messages.insert(insert_at, {'role': hint_role, 'content': RANDOM_REPLY_SYSTEM_PROMPT})
        return messages

    @staticmethod
    def _is_silent_reply(text: str) -> bool:
        """随机插话的「不发言」判定：空正文或模型只回 NO_REPLY 哨兵。

        :param text: 已剥离思维链并 strip 的回复正文
        :return: 是否应保持沉默
        """
        stripped = (text or '').strip()
        if not stripped:
            return True
        return NO_REPLY_PATTERN.match(stripped) is not None

    async def _extract_user_image_visions(self, event: MessageEvent) -> list[dict]:
        """识别并下载用户消息中携带的图片，返回视觉输入信息列表。

        遍历消息中的 Image 消息段，取其图片地址（``url`` 或 ``file``），经
        ``tools.fetch_image_data_url`` 下载并校验为 OpenAI ``image_url`` 可用的
        data URL。下载失败或非受支持格式的图片会被忽略，不影响文本部分的处理。

        :param event: 消息事件
        :return: 视觉信息 dict 列表（含 data_url / mime_type / size / source）；无图片时为 []
        """
        image_segments = event.message.filter_image()
        if not image_segments:
            return []
        visions: list[dict] = []
        for segment in image_segments:
            source = segment.url or segment.file
            if not source:
                _log.debug('图片消息段缺少可下载地址，已忽略')
                continue
            vision = await tools.fetch_image_data_url(source)
            if vision is None:
                _log.warning(f'无法识别或下载用户发送的图片，已忽略: {source}')
                continue
            visions.append(vision)
        return visions

    def _is_trpg_session_active(self, event: MessageEvent, is_group: bool) -> bool:
        """查询 NcatBotTRPG 是否在该会话中开团，用于避免 @机器人 时重复回复

        通过框架的跨插件访问接口 `self.get_plugin()` 获取 NcatBotTRPG 实例；
        插件不存在、未提供查询方法或查询异常时返回 False，不影响本插件独立工作。

        :param event: 消息事件
        :param is_group: 是否群消息
        :return: 跑团是否进行中
        """
        try:
            trpg = self.get_plugin('NcatBotTRPG')
        except Exception as e:
            _log.debug(f'访问 NcatBotTRPG 插件失败: {e}')
            return False
        if trpg is None:
            return False
        try:
            if is_group:
                checker = getattr(trpg, 'is_group_session_active', None)
                if callable(checker) and checker(event.group_id):
                    return True
            else:
                checker = getattr(trpg, 'is_user_session_active', None)
                if callable(checker) and checker(event.user_id):
                    return True
        except Exception as e:
            _log.debug(f'查询 NcatBotTRPG 会话状态失败: {e}')
        return False

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

        # 不处理机器人自己发出的消息，避免自问自答循环
        if str(event.user_id) == str(event.self_id):
            return

        # 检查是否已配置插件
        if not self.get_config('IsConfigured'):
            # _log.warning('插件未配置，请先配置插件后再使用')
            return

        # 与 NcatBotTRPG 协调：该会话跑团进行中时，消息交由跑团插件接管，避免重复回复
        if self._is_trpg_session_active(event, is_group):
            _log.debug('该会话跑团进行中，跳过 OpenAI 对话处理（交由 NcatBotTRPG 接管）')
            return

        user_message = await self._build_user_message(event)

        random_reply = False
        if is_group:  # 群消息
            conversation_dict = 'group_conversations'
            session_id = event.group_id

            at_bot = any(
                isinstance(segment, At) and segment.user_id == str(event.self_id)
                for segment in event.message
            )

            # 未被 @ 时按概率决定是否随机插话
            if not at_bot:
                random_reply = self._should_random_reply(event)
                if random_reply:
                    _log.info(f'[群组 {session_id}] 命中随机插话概率，本次消息将参与群聊')

            # 检查是否必须@机器人才能触发对话（随机插话命中时绕过该限制）
            if self.get_config('MustAtBot') and not at_bot and not random_reply:
                _log.debug('群消息未@机器人，忽略该消息')
                return
            _log.debug('群消息已@机器人，处理该消息' if at_bot else '群消息随机插话，处理该消息')
        else:  # 私聊消息
            conversation_dict = 'user_conversations'
            session_id = event.user_id

        # 检查会话是否存在，不存在则用默认预设初始化
        if session_id not in self.data['data'][conversation_dict]:
            default_preset = Preset()
            if not default_preset.load(str(self.workspace), DEFAULT_PRESET_NAME):
                _log.error('默认预设不存在，无法初始化会话')
                return
            self.data['data'][conversation_dict][session_id] = default_preset.to_conversations()
            self._set_preset_name(conversation_dict, session_id, DEFAULT_PRESET_NAME)

        # 识别用户消息中携带的图片（可配置关闭），作为视觉输入交给模型
        user_vision_infos: list[dict] = []
        if self.get_config('EnableImageRecognition'):
            user_vision_infos = await self._extract_user_image_visions(event)

        # 构建写入会话的用户消息内容：
        # - 无图片：保持纯文本字符串（与旧行为一致）
        # - 有图片：转为 OpenAI 多模态列表（文本段 + 一个或多个 image_url 段）
        if user_vision_infos:
            # 仅取纯文本段作为文本部分，避免把 [CQ:image,...] 等消息段标记塞给模型；
            # 仅发图片无文字时给一句默认说明。
            clean_text = event.message.text.strip()
            text_part = clean_text or '用户发送了图片，请查看随附图片。'
            if self.get_config('InsertUserdataAsPrefix'):
                text_part = f'{event.sender.nickname}({event.sender.user_id}): {text_part}'
            user_content: str | list[dict] = [{'type': 'text', 'text': text_part}]
            for vision in user_vision_infos:
                user_content.append({'type': 'image_url', 'image_url': {'url': vision['data_url']}})
            _log.info(
                f'[{"群组" if is_group else "用户"} {session_id}] 识别到 {len(user_vision_infos)} 张用户图片，'
                f'将作为视觉输入附加到对话')
        else:
            user_content = user_message

        # 添加用户消息到会话
        self.data['data'][conversation_dict][session_id].append({'role': 'user', 'content': user_content})
        _log.info(
            f'[{"群组" if is_group else "用户"} {session_id}] 用户输入: '
            f'{user_message[:OMITTED_TEXT_LENGTH]}{"..." if len(user_message) > OMITTED_TEXT_LENGTH else ""}')

        response = None
        try:
            current_retries_times = 0
            # 随机插话场景禁用工具调用：省 token，且避免随机消息产生副作用
            use_tools = self.get_config('EnableBuiltinFunctionCalling') and not random_reply
            available_tools = tools.get_available_tools(
                allow_scheduled_tasks=self.get_config('AllowScheduledTasks')) if use_tools else None

            # 如果启用了内置函数调用功能，则在模型想要调用工具时会循环执行工具调用并获取结果，
            # 直到模型不再想要调用工具或达到最大重试次数为止
            while current_retries_times < self.get_config('MaxRetriesTimes'):
                response = self._default_client.chat.completions.create(
                    model=self.get_config('Model'),
                    messages=self._build_request_messages(
                        self.data['data'][conversation_dict][session_id], random_reply),
                    tools=available_tools,
                    tool_choice='auto' if use_tools else 'none',
                )

                _log.debug(
                    f'请求尝试: {current_retries_times + 1}/{self.get_config("MaxRetriesTimes")}，'
                    f'模型回复: {response.choices[0].message.content}, '
                    f'finish_reason: {response.choices[0].finish_reason}, '
                    f'tool_calls: {response.choices[0].message.tool_calls}'
                )

                # 思维链只写日志，绝不进入回复与会话历史
                self._log_reasoning(
                    response.choices[0].message, response.choices[0].message.content or '')

                # 模型是否主动停止生成回复
                if response.choices[0].finish_reason == 'stop':
                    _log.debug('模型回复已完成，无需继续处理工具调用。')
                    break

                # 检查模型是否想要调用工具
                if use_tools:
                    if response.choices[0].message.tool_calls:
                        current_retries_times += 1
                        thinking_content = self._clean_content(response.choices[0].message.content)
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
                        # 仅在正文存在有效内容时发送；「仅空白/空」的中间内容
                        # （如换行、空格）视为无效，不向用户发出（复用上方已 strip 的 thinking_content）
                        if thinking_content:
                            if is_group:
                                await self.api.qq.post_group_msg(event.group_id, text=thinking_content)
                            else:
                                await self.api.qq.post_private_msg(event.user_id, text=thinking_content)

                        # 处理每个工具调用请求
                        preset_name = self._get_preset_name(conversation_dict, session_id)
                        vision_messages: list[dict] = []
                        for tool_call in response.choices[0].message.tool_calls:
                            tool_name = tool_call.function.name
                            try:
                                tool_args = json.loads(tool_call.function.arguments or '{}')
                            except ValueError:
                                tool_args = {}
                                _log.warning(f'工具参数不是有效的 JSON: {tool_call.function.arguments}')

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
                                        os.path.join(str(self.workspace), 'presets', preset_name), **tool_args
                                    )
                            elif tool_name == 'create_http_request':
                                if not self.get_config('AllowWebRequests'):
                                    # 如果不允许网络请求功能，则拒绝工具调用请求并返回错误信息
                                    result = tools._generate_tool_payload(
                                        'error', '`AllowWebRequests` 配置未启用，无法使用网络请求功能')
                                    _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许网络请求功能')
                                else:
                                    result = await tools.create_http_request(**tool_args)
                            elif tool_name == 'manage_cron_tasks':
                                if not self.get_config('AllowScheduledTasks'):
                                    # 双保险：未开启时即使模型看到 schema 也无法创建任务
                                    result = tools._generate_tool_payload(
                                        'error', '`AllowScheduledTasks` 配置未启用，无法管理定时任务')
                                    _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许定时任务功能')
                                else:
                                    # 投递目标强制为当前会话，模型无法指定其他群/用户
                                    result = await tools.manage_cron_tasks(
                                        self,
                                        session_type='group' if is_group else 'private',
                                        session_id=str(session_id),
                                        **tool_args,
                                    )
                            else:
                                _log.warning(f'未知工具调用请求: {tool_name}')
                                result = tools._generate_tool_payload('error', f'未知工具: {tool_name}')

                            # 图片类工具结果：工具历史只保存摘要，完整 data URL 作为
                            # 后续 user 视觉消息附加，避免把 base64 正文塞进上下文
                            vision = tools.extract_vision_payload(result)
                            history_content = tools._vision_summary_payload(vision) if vision else result

                            _log.info(
                                f'[{"群组" if is_group else "用户"} {session_id}] 工具调用: '
                                f'{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:OMITTED_TEXT_LENGTH]}) -> '
                                f'{str(history_content)[:OMITTED_TEXT_LENGTH]}'
                                f'{"..." if len(str(history_content)) > OMITTED_TEXT_LENGTH else ""}'
                            )

                            # 将工具调用结果添加到会话中，供模型后续生成回复时参考
                            self.data['data'][conversation_dict][session_id].append(
                                {'tool_call_id': tool_call.id, 'role': 'tool', 'name': tool_name,
                                 'content': history_content})

                            if vision:
                                vision_messages.append({
                                    'role': 'user',
                                    'content': [
                                        {'type': 'text', 'text': '以下是通过工具获取的图片：'},
                                        {'type': 'image_url', 'image_url': {'url': vision['data_url']}},
                                    ],
                                })

                        # 所有 tool 消息追加完毕后，再附加视觉消息（保持 tool_calls 与 tool 的相邻顺序）
                        for vision_message in vision_messages:
                            self.data['data'][conversation_dict][session_id].append(vision_message)
                            _log.debug('已将工具获取的图片作为视觉输入附加到会话')
                    else:
                        break

            # MaxRetriesTimes 配置 <= 0 时循环不会执行，避免后续解引用未定义的 response
            if response is None:
                _log.error('MaxRetriesTimes 配置须为正整数，已跳过本次对话请求')
                return

            last_msg = response.choices[0].message
            # 模型可能在正文首尾输出换行/空格（日志中常见 "\n\n你好..."），发送前统一去除；
            # 同时剥离思维链，避免把推理过程发送给用户
            reply_message = self._clean_content(last_msg.content)

            # 随机插话：模型回空正文或 NO_REPLY 哨兵时保持沉默（不发送、不写 assistant 历史）
            if random_reply and self._is_silent_reply(reply_message):
                _log.info(f'[群组 {session_id}] 随机插话未命中/模型选择不发言，已跳过发送')
                self._save_data()
                return

            # 最后一轮 API 仍在请求工具时 while 已无法继续，content 往往为空，避免 reply(None)
            if last_msg.tool_calls and not reply_message:
                raise exceptions.TooManyToolCallsException('抱歉，连续工具调用次数已达上限')
            if not reply_message:
                _log.warning('模型返回了空回复，已跳过发送')
                self._save_data()
                return

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
            f'[群组 {group_key}:{event.user_id}] 记录消息: '
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
