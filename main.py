# -*- coding: utf-8 -*-
import asyncio
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
from openai import BadRequestError, OpenAI, RateLimitError

from . import exceptions, reasoning, tools
from .command_handler import (
    ADMIN_PERMISSION,
    DEFAULT_PRESET_NAME,
    OpenAICommandHandlerMixin,
)
from .preset_manager import Preset
from .prompt import (
    IMAGE_ONLY_USER_TEXT,
    IMAGE_PLACEHOLDER_TEXT,
    RANDOM_REPLY_CONTEXT_HEADER,
    RANDOM_REPLY_SYSTEM_PROMPT,
    TOOL_VISION_IMAGE_TEXT,
    SCHEDULED_TASK_MESSAGE_TEMPLATE,
    CONVERSATION_COMPRESSION_SYSTEM_PROMPT,
    CONVERSATION_COMPRESSION_USER_TEMPLATE,
)
from .session_store import SessionStore
from .update import (
    migrate_conversations_to_split_files,
    migrate_legacy_workspace,
    migrate_memory_format,
    migrate_presents_dir,
    needs_conversation_split_migration,
    needs_memory_migration,
)

_log = get_log('openai_chat_plugin')  # 日志记录器

# 省略文本长度
OMITTED_TEXT_LENGTH = 100

# 模型「不发言」哨兵（所有对话轮通用）：容忍 NO_REPLY / NO-REPLY / `no_reply` 等写法
NO_REPLY_PATTERN = re.compile(r'^\W*no[_\-\s]?reply\W*$', re.IGNORECASE)
# 沉默决定写入会话历史时的规范化文本：哨兵变体统一记为 NO_REPLY
NO_REPLY_SENTINEL = 'NO_REPLY'
# 同一群的随机插话最小间隔（秒），防止连续刷屏
RANDOM_REPLY_COOLDOWN_SECONDS = 30
# 随机插话提示里附带的历史群聊条数上限：给模型提供「大家在聊什么」的上下文，
# 避免它因为看不到群聊历史而反复调用记忆工具、或直接判定没什么可说
RANDOM_REPLY_CONTEXT_MESSAGES = 20
# 上下文里单条群消息的字符上限：群友可能粘贴长文，不能让一条消息挤占整个上下文
RANDOM_REPLY_CONTEXT_LINE_CHARS = 200
# 单次 OpenAI 请求超时（秒）。同步客户端在网络异常时可能长时间阻塞，
# 因此限制单请求耗时并减少重试，避免定时任务卡住事件循环太久
REQUEST_TIMEOUT_SECONDS = 60.0
# 请求限流/服务端错误的最大重试次数（SDK 会对 408/409/429/5xx 做指数退避，
# 并遵守响应头 Retry-After）。生产环境出现过瞬时 429：只重试 1 次时，第二次
# 仍被限流就会把异常抛给用户，因此放宽到 4 次（最多 5 次尝试）
REQUEST_MAX_RETRIES = 4

# 会话压缩生成摘要时的输入预算（字符）与单条消息的保留上限。
# 被压缩的历史超出预算时**等距抽样**而不是只截取开头，保证摘要覆盖整段历史。
SUMMARY_INPUT_BUDGET_CHARS = 12000
SUMMARY_LINE_MAX_CHARS = 1000
# 自动清理循环的默认检查间隔（秒）：配置为 0/负数/非法值时回退到该值，避免忙等扫描磁盘
AUTO_CLEANUP_DEFAULT_INTERVAL_SECONDS = 1800.0


class OpenAIChatPlugin(OpenAICommandHandlerMixin, NcatBotPlugin):
    """OpenAI 对话插件（NcatBot 5）

    通过 OpenAI API 实现智能对话，支持多预设管理、函数调用（Function Calling）、会话持久化。
    """

    name = 'OpenAIChatPlugin'  # 插件名（须与 manifest.toml 一致）
    version = '0.5.2'  # 插件版本
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
            'AllowAccessMemory': False,  # 是否允许AI记录和访问会话历史（内置函数调用功能需要开启）
            'AllowWebRequests': False,  # 是否允许AI进行网络请求（内置函数调用功能需要开启）
            'AllowScheduledTasks': False,  # 是否允许AI创建/管理定时任务（内置函数调用功能需要开启）
            # 定时任务执行总开关（kill switch）：关闭后不注册/不执行任务，
            # 记录保留，可用 /chat-admin task resume 恢复
            'EnableScheduledTaskExecution': True,
            'EnableImageRecognition': True,  # 是否识别并下载用户消息中携带的图片，作为视觉输入交给模型（无需开启函数调用）
            'StripReasoning': True,  # 是否剥离推理模型的思维链（<think>/<reasoning> 等标签），防止思维链暴露给用户或污染历史
            'MaxRetriesTimes': 15,  # 启用内置函数调用功能时，模型调用工具后重新生成回复的最大重试次数
            'MaxConversationCacheStoreAmount': 1000,  # 最大对话缓存存储数量，用于AI获取非唤醒时错过的上下文消息，超过该数量的最早消息将被丢弃
            'IsConfigured': False,  # 插件是否已配置
            'EnableGroupOwnerAutoAuth': True,  # 群主/群管理自动放行本群管理员命令（/chat-admin）
            'EnableRandomReplyGroup': False,  # 是否随机回复群消息
            # 'AllowHttpRiskIps': False,  # 是否允许风险IP段访问（如AWS 169.254.169.254）（直接禁止访问，不做配置）
            'AllowLoopbackIps': False,  # 是否允许回环访问（如 127.0.0.1）
            'AllowLocalNetworkIps': False,  # 是否允许访问局域网内网IP段（如 192.168.0.0/16）
            'GroupRandomReplyChance': 0.02,  # 群随机回复概率（0~1）
            # 会话压缩配置
            'EnableConversationCompression': False,  # 是否启用会话压缩（超阈值历史压缩为摘要）
            # 自动清理（后台循环）总开关：与 EnableConversationCompression 任一开启即启动循环
            'EnableAutoCleanup': False,
            'AutoCleanupInterval': 1800,  # 自动清理检查间隔（秒），默认 30 分钟；<=0 回退默认值
            'AutoCleanupIdleSeconds': 28800,  # 会话闲置多久才允许自动压缩（秒），默认 8 小时
            'CompressionThresholdBytes': 1048576,  # 压缩阈值（字节），默认 1MB（约 350k 中文字符，需按模型上下文调整）
            'CompressionKeepRecentBytes': 524288,   # 保留最近的会话大小（字节），默认 512KB
        })

        # ---- RBAC：注册管理员权限点并自动授予 root（幂等） ----
        self.add_permission(ADMIN_PERMISSION)
        root = get_config_manager().config.root
        if root and self.rbac and not self.check_permission(root, ADMIN_PERMISSION):
            self.rbac.grant('user', root, ADMIN_PERMISSION)
            _log.info(f'已自动授予 root({root}) 插件全局管理员权限')

        # ---- 初始化持久化数据结构（逐字段补齐，兼容旧版本数据缺字段的情况） ----
        # 注：group_conversations / user_conversations 自 v0.5.0 起由 SessionStore
        # 按会话独立文件（group|user/<id>.json）管理，不再放在 data.json 中；
        # 旧版集中存储的数据由会话拆分迁移（见下文）搬走
        self.data.setdefault('data', {
            'group_preset_names': {},
            'user_preset_names': {},
            # 群级随机插话开关：group_id -> bool（True = 该群不再随机插话）
            'group_nolisten': {},
            # AI 定时任务记录：task_name -> {interval, prompt, session_type, session_id, created_at}
            'scheduled_tasks': {}
        })
        for key in ('group_preset_names', 'user_preset_names', 'group_nolisten',
                    'scheduled_tasks'):
            self.data['data'].setdefault(key, {})

        # 准备历史信息查询缓存 kv（query_missing_message_context 工具使用）
        self.temp_history_messages_kv = {}
        # 随机插话冷却时间记录（内存态，不持久化）：group_id -> monotonic 时间戳
        self._last_random_reply_ts: dict[str, float] = {}
        # 视觉能力探测（内存态）：供应商返回「content.type 非法/仅支持 text」后置 True，
        # 之后请求自动压平多模态内容并停止下载用户图片（纯文本模型兼容）
        self._vision_unsupported = False
        # 会话级锁（内存态）：普通聊天与定时任务共用，保证同一会话的轮次串行执行
        self._session_locks: dict[tuple[str, str], asyncio.Lock] = {}
        # 会话历史存储（分离文件：<workspace>/group|user/<id>.json）
        self._sessions = SessionStore(self.workspace)

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

        # ---- 会话数据拆分迁移（data.json 集中存储 -> group|user/<id>.json 分离文件，失败不阻塞加载） ----
        # 必须排在 migrate_legacy_workspace 之后：4.x 数据先汇入 data.json，再由这里拆分
        try:
            if needs_conversation_split_migration(self):
                _log.info('检测到 data.json 中仍存在集中存储的会话数据，正在拆分到分离文件...')
                if migrate_conversations_to_split_files(self):
                    _log.info('已将会话数据拆分到 group/、user/ 分离文件')
            else:
                _log.debug('未检测到需要拆分的会话数据，跳过拆分迁移')
        except Exception as e:
            _log.error(f'会话数据拆分迁移失败（旧数据仍保留在 data.json，下次启动重试）: {e}')

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
            base_url=self.get_config('BaseUrl'),
            timeout=REQUEST_TIMEOUT_SECONDS,
            max_retries=REQUEST_MAX_RETRIES,
        )

        # ---- 恢复持久化的 AI 定时任务（失败不阻塞加载） ----
        self._restore_scheduled_tasks()

        # 启动会话压缩后台循环（异步任务）。
        # 压缩只能由该循环触发，因此 EnableConversationCompression 单独开启也启动；
        # EnableAutoCleanup 是兼容用的总开关，两者任一开启即可。
        # 任务引用必须保留（否则可能被 GC），并在 on_close() 中取消
        self._cleanup_task: asyncio.Task | None = None
        if self._cleanup_enabled():
            self._cleanup_task = asyncio.create_task(self._auto_cleanup_loop())

    async def on_close(self):
        """插件卸载时取消后台清理循环，避免热重载后留下孤儿任务/重复压缩。"""
        task = getattr(self, '_cleanup_task', None)
        self._cleanup_task = None
        if task is None or task.done():
            return
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        except Exception as e:  # 任务内部异常不应阻塞卸载
            _log.warning(f'后台会话压缩任务退出异常: {e}')

    def _restore_scheduled_tasks(self) -> int:
        """把 data.json 中记录的定时任务重新注册到框架调度器。

        任务记录不会因为 ``AllowScheduledTasks`` 关闭而删除，该开关只控制模型能否
        新增/管理任务；注册失败时保留记录并记录警告，便于排查 interval 等问题。
        执行总开关 ``EnableScheduledTaskExecution`` 关闭或记录被标记为
        ``disabled`` 时不会注册，已在运行的调度任务需由调用方先移除。

        :return: 成功注册的任务数量
        """
        tasks = self.data['data'].get('scheduled_tasks') or {}
        if not tasks:
            return 0
        if not self.get_config('EnableScheduledTaskExecution', True):
            _log.info('定时任务执行开关已关闭，跳过恢复注册')
            return 0
        restored = 0
        for task_name, info in tasks.items():
            if not isinstance(info, dict):
                continue
            if info.get('disabled'):
                _log.info(f'定时任务 {task_name} 已停用，跳过恢复（可用 /chat-admin task delete 清理）')
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
                reason = tools.validate_schedule_interval(info.get('interval', ''))
                _log.warning(
                    f'定时任务 {task_name} 恢复失败（{reason or "定时任务服务不可用"}）')
        if restored:
            _log.info(f'已恢复 {restored}/{len(tasks)} 个 AI 定时任务')
        return restored

    def _save_session(self, conversation_dict: str, session_id) -> None:
        """持久化单个会话及其关联状态。

        会话消息由 ``SessionStore`` 写入分离文件；同时 ``_save_data()`` 落盘
        data.json 中与此会话相关的状态（``*_preset_names`` 等）。

        :param conversation_dict: ``group_conversations`` 或 ``user_conversations``
        :param session_id: 会话 ID
        """
        self._sessions.save(conversation_dict, str(session_id))
        self._save_data()

    def _ensure_session(self, conversation_dict: str, session_id: str) -> list[dict]:
        """确保会话存在，返回该会话的消息列表（不存在时用默认预设初始化）。

        会话消息存于分离文件（``<workspace>/group|user/<id>.json``，v0.5.0+）。

        :param conversation_dict: ``group_conversations`` 或 ``user_conversations``
        :param session_id: 会话 ID（统一转为 str）
        :return: 会话消息列表（原地修改即可持久化，需随后调用 _save_session）
        """
        session_key = str(session_id)
        existing = self._sessions.load(conversation_dict, session_key)
        if existing is not None:
            return existing
        preset = Preset()
        if preset.load(str(self.workspace), DEFAULT_PRESET_NAME):
            messages = self._sessions.replace(
                conversation_dict, session_key, preset.to_conversations())
            self._set_preset_name(conversation_dict, session_key, DEFAULT_PRESET_NAME)
        else:
            # 预设缺失时不阻塞定时任务：退化为无 system 提示的会话
            _log.warning('默认预设不存在，会话将以无 system 提示的方式初始化')
            messages = self._sessions.replace(conversation_dict, session_key, [])
        return messages

    async def _run_scheduled_task_turn(self, session_type: str, session_id: str,
                                       task_name: str, prompt: str,
                                       creator_id: str = '') -> str:
        """执行一次定时任务轮次（内部消息入主会话历史 → 模型/工具 → 投递 → 落库）。

        定时任务与普通聊天共用同一份会话历史，并且同样具备工具调用能力（受
        ``EnableBuiltinFunctionCalling`` 与各 ``AllowXxx`` 开关控制）。请求在内存中
        构造为 ``历史 + 内部消息``，只有最终回复成功投递后才把整轮消息写入历史，
        失败不会污染上下文。模型主动选择沉默（回复 ``NO_REPLY`` 哨兵）时同样把
        整轮消息写入历史（归一化的沉默决定），并视为执行成功、不计失败。

        :param session_type: 'group' 或 'private'
        :param session_id: 投递目标会话 ID
        :param task_name: 任务名（不含插件前缀）
        :param prompt: 任务提示
        :param creator_id: 任务创建者 ID（供记忆/环境工具使用，旧记录可能为空）
        :return: 已投递的回复正文；模型选择沉默时返回 ``NO_REPLY``（不投递）
        :raises RuntimeError: 客户端未初始化、模型返回空内容或投递失败
        """
        conversation_dict = ('group_conversations' if session_type == 'group'
                             else 'user_conversations')
        session_key = str(session_id)
        is_group = session_type == 'group'
        internal_message = {
            'role': 'user',
            'content': SCHEDULED_TASK_MESSAGE_TEMPLATE.format(
                task_name=task_name, prompt=prompt),
        }

        if getattr(self, '_default_client', None) is None:
            raise RuntimeError('OpenAI 客户端未初始化')

        async with self._get_session_lock(conversation_dict, session_key):
            history = self._ensure_session(conversation_dict, session_key)
            # 工作副本：历史 + 内部触发消息；工具轮次只会追加到副本
            working = list(history) + [internal_message]
            reply = await self._generate_reply(
                working,
                conversation_dict=conversation_dict,
                session_id=session_key,
                is_group=is_group,
                sender_id=str(creator_id or ''),
                event=None,
                random_reply=False,
                use_tools=bool(self.get_config('EnableBuiltinFunctionCalling')),
            )
            # 模型主动选择沉默（NO_REPLY 哨兵）：不投递，但整轮消息（内部消息、
            # 工具轮、归一化的沉默决定）写入历史——沉默是有效决定而非失败，
            # 不累计 failures；空正文（未产生任何内容）仍算失败
            if self._is_no_reply_sentinel(reply):
                working.append({'role': 'assistant', 'content': NO_REPLY_SENTINEL})
                history[:] = working
                self._save_session(conversation_dict, session_key)
                _log.info(
                    f'{self._session_tag(is_group, session_key)} '
                    f'定时任务 {task_name} 模型选择不发言（NO_REPLY），未投递')
                return NO_REPLY_SENTINEL

            if not reply:
                raise RuntimeError('模型返回空内容')

            # 先投递最终回复，成功后再把整轮消息（含工具调用）提交回历史
            await self._post_session_text(session_key, is_group, reply)
            working.append({'role': 'assistant', 'content': reply})
            history[:] = working
            self._save_session(conversation_dict, session_key)

        _log.info(
            f'{self._session_tag(is_group, session_key)} '
            f'定时任务 {task_name} 已投递（{len(reply)} 字）')
        return reply

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

    def _recent_group_messages(self, conversation: list[dict],
                               group_id: str) -> list[str]:
        """取最近若干条群聊记录（不含已在会话历史中的消息）。

        随机插话需要「大家在聊什么」才能判断是否值得接话。群会话历史里只保留
        与机器人相关的轮次，其他人的日常发言在 ``temp_history_messages_kv``；
        已经在会话历史里的内容不再重复附上，避免同一句话出现两遍。

        :param conversation: 当前会话消息列表（末尾为本次用户消息）
        :param group_id: 群号
        :return: 最近的群聊消息文本（最多 ``RANDOM_REPLY_CONTEXT_MESSAGES`` 条，
                 单条最多 ``RANDOM_REPLY_CONTEXT_LINE_CHARS`` 字符；含 ``NO_REPLY``
                 的消息会被剔除，避免群友的文本影响沉默哨兵）
        """
        history = self.temp_history_messages_kv.get(str(group_id)) or []
        known = {
            message.get('content')
            for message in conversation
            if message.get('role') == 'user' and isinstance(message.get('content'), str)
        }
        candidates = [
            text[:RANDOM_REPLY_CONTEXT_LINE_CHARS]
            for text in history
            # 群友自己打出 NO_REPLY 不应影响模型对哨兵的理解，也不能挤进提示里
            if text not in known and 'NO_REPLY' not in text.upper()
        ]
        return candidates[-RANDOM_REPLY_CONTEXT_MESSAGES:]

    def _build_random_reply_hint(self, conversation: list[dict], group_id: str,
                                 use_tools: bool) -> str:
        """组装随机插话的临时提示。

        函数调用可用时只给提示，由模型自己调用 ``query_missing_message_context``
        查看被忽略的群消息；不可用时才由插件附带固定长度的群聊记录兜底。

        :param conversation: 当前会话消息列表（末尾为本次用户消息）
        :param group_id: 群号
        :param use_tools: 本次请求是否启用工具调用
        :return: 注入到请求副本中的 user 消息内容
        """
        if use_tools:
            return RANDOM_REPLY_SYSTEM_PROMPT
        context_lines = self._recent_group_messages(conversation, group_id)
        if not context_lines:
            return RANDOM_REPLY_SYSTEM_PROMPT
        return (f'{RANDOM_REPLY_SYSTEM_PROMPT}\n\n{RANDOM_REPLY_CONTEXT_HEADER}\n'
                + '\n'.join(context_lines))

    def _build_request_messages(self, conversation: list[dict],
                                insert_at: int | None = None,
                                hint: str | None = None) -> list[dict]:
        """构造本次 API 请求的消息列表（副本，不写回会话历史）。

        随机插话时插入一条临时提示，固定使用 user 角色。不用 system 是因为
        部分网关（如 AMD Radeon、智谱 GLM）只接受位于消息列表开头的单个
        system，第二条 system 会被 400 拒绝
        （'System message must be at the beginning.'）。

        提示插在**本次用户消息之前**（而非 system 之后）：既让指令贴近最新
        消息、避免长历史下被稀释，也保留 system 之后的历史前缀缓存；同时不会
        把提示伪装成早先的独立用户轮次。

        ``insert_at`` 必须由调用方按「本次用户消息在列表中的下标」给出，并在
        工具循环中保持不变：工具轮会不断往末尾追加 assistant/tool 消息，若每轮
        都用 ``len - 1`` 重新推断，提示会被插到 assistant(tool_calls) 与 tool
        结果之间，破坏工具消息的相邻顺序（严格网关直接 400），语义上也会变成
        「对工具结果的备注」而不是「对用户消息的插话判断」。

        :param conversation: 会话消息列表
        :param insert_at: 提示插入位置（本次用户消息的下标）；None 表示插在最后一条之前
        :param hint: 提示内容；None 表示普通聊天，不插入任何提示
        :return: 供 API 使用的消息列表副本
        """
        messages = list(conversation)
        if hint is None or not messages:
            return messages
        index = len(messages) - 1 if insert_at is None else insert_at
        index = max(0, min(index, len(messages)))
        messages.insert(index, {'role': 'user', 'content': hint})
        return messages

    @staticmethod
    def _content_to_text_only(content):
        """把多模态 content 列表压平成纯文本（图片替换为 ``[图片]`` 占位符）。

        纯文本模型/网关（如智谱 GLM 返回 400 code 1210）只接受
        ``content.type == 'text'``，历史里遗留的 ``image_url`` 会让后续每一轮
        请求都失败，因此请求前需要降级。
        """
        if not isinstance(content, list):
            return content
        parts: list[str] = []
        for part in content:
            if not isinstance(part, dict):
                continue
            if part.get('type') == 'text':
                parts.append(part.get('text') or '')
            elif part.get('type') == 'image_url':
                parts.append(IMAGE_PLACEHOLDER_TEXT)
        return '\n'.join(text for text in parts if text) or IMAGE_PLACEHOLDER_TEXT

    @classmethod
    def _text_only_messages(cls, messages: list[dict]) -> list[dict]:
        """返回多模态内容压平后的消息副本（不修改会话历史）。"""
        normalized: list[dict] = []
        for message in messages:
            item = dict(message)
            item['content'] = cls._content_to_text_only(item.get('content'))
            normalized.append(item)
        return normalized

    @staticmethod
    def _is_text_only_content_error(exc: BadRequestError) -> bool:
        """判断 400 是否由「模型不支持多模态 content」导致。

        兼容智谱 GLM 的 ``messages.content.type 参数非法，取值范围 ['text']``
        （错误码 1210）以及 OpenAI 兼容网关的 ``image_url is not supported`` 等文案。
        """
        if str(getattr(exc, 'code', '') or '') == '1210':
            return True
        text = str(exc).lower()
        if 'content.type' in text or 'content type' in text:
            return True
        return 'image_url' in text and ('unsupported' in text or 'not support' in text)

    @staticmethod
    def _is_no_reply_sentinel(text: str) -> bool:
        """模型是否显式回复「不发言」哨兵（所有对话轮通用）。

        :param text: 已剥离思维链并 strip 的回复正文
        :return: 是否命中 NO_REPLY 哨兵（容忍反引号/句点/连字符/大小写变体）
        """
        return NO_REPLY_PATTERN.match((text or '').strip()) is not None

    @staticmethod
    def _commit_working_turn(conversation: list[dict], working: list[dict]) -> None:
        """把随机插话工作副本中的工具轮消息提交回主会话。

        工作副本只在随机插话轮创建（普通轮直接在主会话上追加工具轮），
        提交即把副本相对主会话新增的 assistant/tool 消息接回尾部。
        """
        if working is not conversation:
            conversation.extend(working[len(conversation):])

    @staticmethod
    def _session_tag(is_group: bool, session_id: str) -> str:
        """统一日志里的会话前缀，如 ``[群组 123456]``。"""
        return f'[{"群组" if is_group else "用户"} {session_id}]'

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
            vision = await tools.fetch_image_data_url(
                source,
                allow_loopback_ips=bool(self.get_config('AllowLoopbackIps')),
                allow_local_network_ips=bool(self.get_config('AllowLocalNetworkIps')),
            )
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

    def _session_key(self, event: MessageEvent) -> tuple[str, str]:
        """返回事件对应的会话字典名与会话 ID（统一为 str）。"""
        if isinstance(event, GroupMessageEvent):
            return 'group_conversations', str(event.group_id)
        return 'user_conversations', str(event.user_id)

    def _get_session_lock(self, conversation_dict: str, session_id: str) -> asyncio.Lock:
        """获取会话级锁。

        普通聊天与定时任务共用同一把锁，避免两边同时读写同一份会话历史
        导致消息交错或上下文丢失。
        """
        key = (conversation_dict, str(session_id))
        lock = self._session_locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._session_locks[key] = lock
        return lock

    async def _handle_message(self, event: MessageEvent):
        """处理消息事件（外层：过滤 + 按会话串行）

        :param event: 事件对象
        :return: None
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

        conversation_dict, session_id = self._session_key(event)
        async with self._get_session_lock(conversation_dict, session_id):
            await self._handle_message_turn(event)

    async def _post_session_text(self, session_id: str, is_group: bool, text: str) -> None:
        """把文本投递到指定会话（群聊/私聊）。"""
        if is_group:
            await self.api.qq.post_group_msg(session_id, text=text)
        else:
            await self.api.qq.post_private_msg(session_id, text=text)

    async def _generate_reply(self, working: list[dict], *, conversation_dict: str,
                              session_id: str, is_group: bool, sender_id: str,
                              event: MessageEvent | None, random_reply: bool,
                              use_tools: bool,
                              random_hint: str | None = None) -> str | None:
        """执行模型请求与工具调用循环，返回清理后的最终正文。

        ``working`` 是本轮的工作消息列表：普通聊天直接传主会话列表（工具轮次原地
        追加），定时任务传"历史副本 + 内部消息"，只有调用方投递成功后才提交回历史。

        :param working: 本轮工作消息列表（会被原地追加 assistant/tool/视觉消息）
        :param conversation_dict: 会话字典名（用于读取预设名）
        :param session_id: 会话 ID
        :param is_group: 是否群聊
        :param sender_id: 触发者 ID（定时任务为创建者，未知时为空）
        :param event: 普通聊天的消息事件；定时任务为 None
        :param random_reply: 是否随机插话轮
        :param use_tools: 是否允许工具调用
        :param random_hint: 随机插话的临时提示（含最近的群聊记录）；普通聊天为 None
        :return: 最终回复正文；模型未产生响应（MaxRetriesTimes 非正）时返回 None
        :raises exceptions.TooManyToolCallsException: 达到工具轮次上限且没有正文
        """
        # 工具 schema 始终完整暴露：各工具开关只影响实际调用（分派链校验 + 错误提示）
        available_tools = tools.tools if use_tools else None
        response = None
        current_retries_times = 0

        # 提示的插入位置在循环前固定为「本次用户消息」的下标：工具轮会往 working
        # 末尾追加 assistant/tool 消息，若每轮按 len-1 重新推断，提示会被插进
        # assistant(tool_calls) 与 tool 结果之间（见 _build_request_messages）
        random_hint_at = len(working) - 1 if random_hint is not None and working else None

        # 如果启用了内置函数调用功能，则在模型想要调用工具时会循环执行工具调用并获取结果，
        # 直到模型不再想要调用工具或达到最大重试次数为止
        while current_retries_times < self.get_config('MaxRetriesTimes'):
            # 未启用工具时完全不传 tools/tool_choice，避免 `"tools": null`
            # 被严格网关（如 AMD Radeon）判为非法请求
            request_kwargs = {
                'model': self.get_config('Model'),
                'messages': self._build_request_messages(
                    working, insert_at=random_hint_at, hint=random_hint),
            }
            # 已确认模型不支持视觉：后续请求直接压平多模态内容，避免每轮都吃 400
            if self._vision_unsupported:
                request_kwargs['messages'] = self._text_only_messages(
                    request_kwargs['messages'])
            if available_tools:
                request_kwargs['tools'] = available_tools
                request_kwargs['tool_choice'] = 'auto'

            # 同步 SDK 调用放到线程池执行，避免阻塞事件循环导致
            # NapCat 心跳超时/其他会话卡死（定时任务同样走 to_thread）
            try:
                response = await asyncio.to_thread(
                    self._default_client.chat.completions.create, **request_kwargs)
            except BadRequestError as e:
                # 纯文本模型遇到历史里的 image_url 会 400：降级为纯文本重试一次，
                # 并记住该模型不支持视觉（后续轮次不再下载/发送图片）
                if self._vision_unsupported or not self._is_text_only_content_error(e):
                    raise
                _log.warning(
                    f'模型不支持多模态 content，已自动降级为纯文本重试: {e}')
                self._vision_unsupported = True
                request_kwargs['messages'] = self._text_only_messages(
                    request_kwargs['messages'])
                response = await asyncio.to_thread(
                    self._default_client.chat.completions.create, **request_kwargs)

            _log.debug(
                f'请求尝试: {current_retries_times + 1}/{self.get_config("MaxRetriesTimes")}，'
                f'模型回复: {response.choices[0].message.content}, '
                f'finish_reason: {response.choices[0].finish_reason}, '
                f'tool_calls: {response.choices[0].message.tool_calls}'
            )

            # 思维链只写日志，绝不进入回复与会话历史
            self._log_reasoning(
                response.choices[0].message, response.choices[0].message.content or '')

            # 模型停止生成、未启用工具，或没有工具调用请求时结束循环
            if (response.choices[0].finish_reason == 'stop'
                    or not use_tools
                    or not response.choices[0].message.tool_calls):
                if response.choices[0].finish_reason == 'stop':
                    _log.debug('模型回复已完成，无需继续处理工具调用。')
                break

            # ---- 工具调用轮 ----
            current_retries_times += 1
            assistant_msg = response.choices[0].message
            thinking_content = self._clean_content(assistant_msg.content)
            if thinking_content:
                _log.info(
                    f'{self._session_tag(is_group, session_id)} '
                    f'AI思考/中间内容: {thinking_content[:OMITTED_TEXT_LENGTH]}'
                    f'{"..." if len(thinking_content) > OMITTED_TEXT_LENGTH else ""}')

            # 完整 assistant 轮次（含 tool_calls）必须先于各条 tool 消息写入历史
            working.append(self._assistant_message_to_history_dict(assistant_msg))

            # 中间正文（如有）也投递到会话；「仅空白/空」视为无效，不发送。
            # 随机插话可能最终选择沉默，中间正文同样不投递，避免「先说话再消失」
            if thinking_content and not random_reply:
                await self._post_session_text(session_id, is_group, thinking_content)

            preset_name = self._get_preset_name(conversation_dict, session_id)
            vision_messages: list[dict] = []
            for tool_call in assistant_msg.tool_calls:
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
                    if event is not None:
                        result = await tools.get_environment_info(event)
                    else:
                        # 定时轮没有消息事件：用餐次上下文替代
                        result = tools._generate_tool_payload('success', '', {
                            'user_id': sender_id or None,
                            'group_id': session_id if is_group else None,
                            'message_id': None,
                            'source': 'scheduled_task',
                        })
                elif tool_name == 'get_stranger_info':
                    result = await tools.get_stranger_info(self.api.qq, **tool_args)
                elif tool_name == 'get_group_info':
                    result = await tools.get_group_info(self.api.qq, **tool_args)
                elif tool_name == 'query_missing_message_context':
                    if is_group:
                        # 取回条数由模型指定，上限强制为缓存配置，防止一次拉回整段历史
                        result = await tools.query_missing_message_context(
                            self.temp_history_messages_kv, session_id,
                            count=tool_args.get('count'),
                            max_count=self.get_config('MaxConversationCacheStoreAmount'))
                    else:
                        result = tools._generate_tool_payload(
                            'error', '查询漏掉的消息上下文仅支持群聊')

                # 以下工具需要配置权限才能调用
                elif tool_name == 'access_memory':
                    if not self.get_config('AllowAccessMemory'):
                        # 如果不允许访问记忆功能，则拒绝工具调用请求并返回错误信息
                        result = tools._generate_tool_payload(
                            'error', '`AllowAccessMemory` 配置未启用，当前无法使用记忆功能')
                        _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许访问记忆功能')
                    else:
                        # 来源由会话/创建者决定（定时任务无创建者时记为未知）
                        tool_args['from_user'] = sender_id or 0
                        tool_args['from_group'] = session_id if is_group else -1
                        result = tools.access_memory(
                            os.path.join(str(self.workspace), 'presets', preset_name), **tool_args
                        )
                elif tool_name == 'create_http_request':
                    if not self.get_config('AllowWebRequests'):
                        # 如果不允许网络请求功能，则拒绝工具调用请求并返回错误信息
                        result = tools._generate_tool_payload(
                            'error', '`AllowWebRequests` 配置未启用，当前无法进行网络请求')
                        _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许网络请求功能')
                    else:
                        # SSRF 防护策略由运营配置决定，模型传入的同名参数一律忽略
                        tool_args.pop('allow_loopback_ips', None)
                        tool_args.pop('allow_local_network_ips', None)
                        result = await tools.create_http_request(
                            allow_loopback_ips=bool(self.get_config('AllowLoopbackIps')),
                            allow_local_network_ips=bool(self.get_config('AllowLocalNetworkIps')),
                            **tool_args,
                        )
                elif tool_name == 'manage_cron_tasks':
                    if not self.get_config('AllowScheduledTasks'):
                        # 工具始终对模型可见，开关关闭时由这里拒绝并给出提示
                        result = tools._generate_tool_payload(
                            'error', '`AllowScheduledTasks` 配置未启用，当前无法管理定时任务')
                        _log.warning(f'工具调用被拒绝: {tool_name}，因为当前预设不允许定时任务功能')
                    else:
                        # 投递目标强制为当前会话，模型无法指定其他群/用户
                        result = await tools.manage_cron_tasks(
                            self,
                            session_type='group' if is_group else 'private',
                            session_id=str(session_id),
                            creator_id=sender_id,
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
                    f'{self._session_tag(is_group, session_id)} 工具调用: '
                    f'{tool_name}({json.dumps(tool_args, ensure_ascii=False)[:OMITTED_TEXT_LENGTH]}) -> '
                    f'{str(history_content)[:OMITTED_TEXT_LENGTH]}'
                    f'{"..." if len(str(history_content)) > OMITTED_TEXT_LENGTH else ""}'
                )

                # 将工具调用结果添加到会话中，供模型后续生成回复时参考
                working.append(
                    {'tool_call_id': tool_call.id, 'role': 'tool', 'name': tool_name,
                     'content': history_content})

                if vision:
                    # 纯文本模型不追加 image_url（否则下一轮请求会 400）；工具历史里
                    # 已保留图片摘要，模型仍知道有图片存在
                    if self._vision_unsupported:
                        _log.debug('模型不支持视觉输入，工具图片已省略（仅保留摘要）')
                    else:
                        vision_messages.append({
                            'role': 'user',
                            'content': [
                                {'type': 'text', 'text': TOOL_VISION_IMAGE_TEXT},
                                {'type': 'image_url', 'image_url': {'url': vision['data_url']}},
                            ],
                        })

            # 所有 tool 消息追加完毕后，再附加视觉消息（保持 tool_calls 与 tool 的相邻顺序）
            for vision_message in vision_messages:
                working.append(vision_message)
                _log.debug('已将工具获取的图片作为视觉输入附加到会话')

        # MaxRetriesTimes 配置 <= 0 时循环不会执行，避免后续解引用未定义的 response
        if response is None:
            _log.error('MaxRetriesTimes 配置须为正整数，已跳过本次对话请求')
            return None

        last_msg = response.choices[0].message
        # 模型可能在正文首尾输出换行/空格（日志中常见 "\n\n你好..."），发送前统一去除；
        # 同时剥离思维链，避免把推理过程发送给用户
        reply_message = self._clean_content(last_msg.content)

        # 最后一轮 API 仍在请求工具时 while 已无法继续，content 往往为空，避免 reply(None)
        if last_msg.tool_calls and not reply_message:
            raise exceptions.TooManyToolCallsException('抱歉，连续工具调用次数已达上限')
        return reply_message

    async def _handle_message_turn(self, event: MessageEvent):
        """处理一轮对话（调用方需持有该会话的锁）

        :param event: 事件对象
        :return: None
        :raises exceptions.TooManyToolCallsException: 当连续工具调用次数达到上限时抛出
        """
        is_group = isinstance(event, GroupMessageEvent)

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

        # 会话不存在或文件损坏（load 返回 None）时用默认预设（重新）初始化。
        # 不能用 has() 判断：文件损坏时 has() 为 True 但 load() 仍返回 None，
        # 后面 append 会抛 AttributeError 让该会话永久不可用
        conversation = self._sessions.load(conversation_dict, session_id)
        if conversation is None:
            default_preset = Preset()
            if not default_preset.load(str(self.workspace), DEFAULT_PRESET_NAME):
                _log.error('默认预设不存在，无法初始化会话')
                return
            conversation = self._sessions.replace(
                conversation_dict, session_id, default_preset.to_conversations())
            self._set_preset_name(conversation_dict, session_id, DEFAULT_PRESET_NAME)

        # 识别用户消息中携带的图片（可配置关闭），作为视觉输入交给模型。
        # 已探测到模型不支持视觉时跳过下载，避免无谓的网络开销
        user_vision_infos: list[dict] = []
        if self.get_config('EnableImageRecognition') and not self._vision_unsupported:
            user_vision_infos = await self._extract_user_image_visions(event)
        elif self._vision_unsupported:
            _log.debug('模型不支持视觉输入，已跳过用户图片下载')

        # 构建写入会话的用户消息内容：
        # - 无图片：保持纯文本字符串（与旧行为一致）
        # - 有图片：转为 OpenAI 多模态列表（文本段 + 一个或多个 image_url 段）
        if user_vision_infos:
            # 仅取纯文本段作为文本部分，避免把 [CQ:image,...] 等消息段标记塞给模型；
            # 仅发图片无文字时给一句默认说明。
            clean_text = event.message.text.strip()
            text_part = clean_text or IMAGE_ONLY_USER_TEXT
            if self.get_config('InsertUserdataAsPrefix'):
                text_part = f'{event.sender.nickname}({event.sender.user_id}): {text_part}'
            user_content: str | list[dict] = [{'type': 'text', 'text': text_part}]
            for vision in user_vision_infos:
                user_content.append({'type': 'image_url', 'image_url': {'url': vision['data_url']}})
            _log.info(
                f'{self._session_tag(is_group, session_id)} 识别到 {len(user_vision_infos)} 张用户图片，'
                f'将作为视觉输入附加到对话')
        else:
            user_content = user_message
            # 模型已确认不支持视觉：图片不会被下载，纯图片消息不能退化成空内容
            if (self._vision_unsupported and event.message.filter_image()
                    and not event.message.text.strip()):
                user_content = IMAGE_PLACEHOLDER_TEXT

        # 添加用户消息到会话
        conversation.append({'role': 'user', 'content': user_content})
        _log.info(
            f'{self._session_tag(is_group, session_id)} 用户输入: '
            f'{user_message[:OMITTED_TEXT_LENGTH]}{"..." if len(user_message) > OMITTED_TEXT_LENGTH else ""}')

        # 随机插话可能最终不发言：工具轮写入工作副本，发言成功或沉默（NO_REPLY）时
        # 提交回主会话；只有异常（限流/工具上限/内部错误）才丢弃，避免失败轮次污染上下文
        working = list(conversation) if random_reply else conversation

        # 随机插话轮同样保留工具能力；副作用类工具仍由各 AllowXxx 开关在分派链拦截
        use_tools = self.get_config('EnableBuiltinFunctionCalling')

        # 有工具时由模型主动调用 query_missing_message_context 查上下文；
        # 只有工具不可用时才由插件附带固定长度的群聊记录兜底
        random_hint = None
        if random_reply:
            random_hint = self._build_random_reply_hint(
                conversation, str(session_id), use_tools)

        try:
            reply_message = await self._generate_reply(
                working,
                conversation_dict=conversation_dict,
                session_id=str(session_id),
                is_group=is_group,
                sender_id=str(event.user_id),
                event=event,
                random_reply=random_reply,
                use_tools=use_tools,
                random_hint=random_hint,
            )

            if reply_message is None:
                return

            # 显式「不发言」哨兵：所有轮次一律不发送，归一化的 NO_REPLY 决定写入
            # 会话历史（工具轮一并提交），保持轮次完整，便于后续对话延续上下文
            if self._is_no_reply_sentinel(reply_message):
                _log.info(
                    f'{self._session_tag(is_group, session_id)} '
                    f'模型选择不发言（NO_REPLY），已跳过发送')
                self._commit_working_turn(conversation, working)
                conversation.append({'role': 'assistant', 'content': NO_REPLY_SENTINEL})
                self._save_session(conversation_dict, session_id)
                return

            # 空正文（如仅返回思维链）：同样不发送；随机插话轮把整轮与归一化的
            # 沉默决定写回历史，普通轮维持「不追加 assistant」的旧行为
            if not reply_message:
                _log.info(
                    f'{self._session_tag(is_group, session_id)} 模型未返回正文，已跳过发送')
                if random_reply:
                    self._commit_working_turn(conversation, working)
                    conversation.append({'role': 'assistant', 'content': NO_REPLY_SENTINEL})
                self._save_session(conversation_dict, session_id)
                return

            _log.info(
                f'{self._session_tag(is_group, session_id)} AI回复: '
                f'{reply_message[:OMITTED_TEXT_LENGTH]}{"..." if len(reply_message) > OMITTED_TEXT_LENGTH else ""}'
            )

            # 回复消息（保持旧版不 @ 发送者的行为）
            await event.reply(reply_message, at_sender=False)

            # 发言成功后才把随机插话轮的工具消息提交回主会话（user 消息此前已写入）
            self._commit_working_turn(conversation, working)

            # 添加AI回复到会话
            conversation.append({'role': 'assistant', 'content': reply_message})

            # 保存持久化文件
            self._save_session(conversation_dict, session_id)
        except exceptions.TooManyToolCallsException as e:
            # 随机插话未 @ 机器人，工具轮上限提示保持静默（只记日志）
            if not random_reply:
                await event.reply(text=str(e), at_sender=False)

        except RateLimitError:
            # SDK 已按 REQUEST_MAX_RETRIES 指数退避重试，仍 429 说明限流持续存在。
            # 随机插话静默放弃，避免往群里发错误提示；@/私聊给出可读提示
            _log.warning(
                f'{self._session_tag(is_group, session_id)} OpenAI 请求触发限流（429），'
                f'本次{"随机插话" if random_reply else "对话"}已放弃')
            self._save_session(conversation_dict, session_id)
            if not random_reply:
                await event.reply(text='请求过于频繁，请稍后再试', at_sender=False)

        except Exception:
            _log.error(traceback.format_exc())
            # 随机插话同样静默，避免未 @ 机器人的消息收到错误提示
            if not random_reply:
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
        """记录群消息到临时历史缓存

        缓存有两处用途：``query_missing_message_context`` 工具，以及随机插话时
        附带的「群聊最近消息」上下文——后者不依赖函数调用开关，因此这里始终记录
        （纯内存，长度受 ``MaxConversationCacheStoreAmount`` 限制）。

        :param event: 群消息事件
        :return: None
        """
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

    # ------------------------------------------------------------------
    # 会话压缩功能
    # ------------------------------------------------------------------

    def _cleanup_enabled(self) -> bool:
        """后台压缩循环是否需要运行：压缩开关或自动清理开关任一开启即可。"""
        return bool(self.get_config('EnableConversationCompression')
                    or self.get_config('EnableAutoCleanup'))

    def _cleanup_interval_seconds(self) -> float:
        """检查间隔（秒）；配置为 0/负数/非法值时回退默认值，避免忙等扫描。"""
        try:
            interval = float(self.get_config('AutoCleanupInterval'))
        except (TypeError, ValueError):
            return AUTO_CLEANUP_DEFAULT_INTERVAL_SECONDS
        return interval if interval > 0 else AUTO_CLEANUP_DEFAULT_INTERVAL_SECONDS

    async def _auto_cleanup_loop(self):
        """自动清理循环：按检查间隔扫描全部会话，仅压缩闲置达标的会话。"""
        while True:
            _log.debug('闲置会话扫描已启动')
            if self._cleanup_enabled():
                await self._auto_cleanup_all()
            # 休眠间隔秒数（检查频率，本身不消耗 API 额度）
            await asyncio.sleep(self._cleanup_interval_seconds())

    async def _auto_cleanup_all(self):
        """执行所有会话的自动清理（静默）。

        只压缩**闲置时间超过 ``AutoCleanupIdleSeconds``** 的会话：以会话文件
        mtime 作为「最后活跃时间」（对话轮次落库 / reset / set-preset 等任何
        会话变动都会写入文件，读操作不更新）。这样活跃会话不会被反复压缩
        消耗 token，也不会在对话进行到一半时被截断。

        压缩前获取会话锁，避免与正在进行的对话轮次 / 定时轮次交错；拿锁后
        再读 mtime，保证判断基于最新状态（在此期间刚落库的轮次会被视为活跃）。
        """
        success_count = 0
        failure_count = 0
        skipped_count = 0
        idle_seconds = self.get_config('AutoCleanupIdleSeconds')

        for conv_type in ['group_conversations', 'user_conversations']:
            # 枚举分离文件中的全部会话（含当前未活跃、仅存在于磁盘的会话）
            for session_id in self._sessions.ids(conv_type):
                session_id = str(session_id)
                # 先拿会话锁再判断闲置：防止与在途轮次竞态（在途轮次落库后
                # mtime 更新，本轮自然跳过）
                async with self._get_session_lock(conv_type, session_id):
                    mtime = self._sessions.mtime(conv_type, session_id)
                    if mtime is not None and (time.time() - mtime) < idle_seconds:
                        skipped_count += 1
                        _log.debug(
                            f'会话 {conv_type}/{session_id} 闲置不足 {idle_seconds}s，跳过自动压缩')
                        continue
                    try:
                        result = await self._compress_conversation(conv_type, session_id)
                        if result == 'compressed':
                            success_count += 1
                        elif result == 'skipped':
                            skipped_count += 1
                        else:
                            failure_count += 1
                    except Exception as e:
                        _log.error(f'压缩会话 {conv_type}/{session_id} 失败：{e}')
                        failure_count += 1

        if success_count > 0:
            _log.info(f'自动清理完成：成功{success_count}个，跳过{skipped_count}个，失败{failure_count}个')

    async def _compress_conversation(self, conversation_dict: str, session_id: str) -> str:
        """压缩单个会话历史。

        规则：

        - 会话首条 ``system`` 提示词（预设人格）**永不参与压缩**，压缩后仍在最前；
        - 保留**尾部** ``CompressionKeepRecentBytes`` 字节的最近消息（至少 1 条），
          只压缩更早的历史；
        - 摘要插在 ``system`` 之后（``_compress_with_summary``）或写入预设记忆
          （``_compress_with_memory``）；
        - 摘要生成 / 记忆写入失败一律返回 ``'failed'``，会话历史保持不变。

        返回：'compressed' | 'skipped' | 'failed'
        """
        # 检查配置
        if not self.get_config('EnableConversationCompression'):
            return 'skipped'

        conv = self._sessions.load(conversation_dict, session_id)
        if conv is None:
            return 'skipped'  # 无会话文件

        # 计算当前会话大小（字节）
        size_bytes = self._calculate_conversation_size(conv, method='bytes')
        threshold = self.get_config('CompressionThresholdBytes', 1048576)
        keep_recent_bytes = self.get_config('CompressionKeepRecentBytes', 524288)

        if size_bytes <= threshold:
            return 'skipped'  # 未达阈值

        # system 提示词不参与压缩；正文至少要有 1 条可压缩 + 1 条保留
        system_count = self._system_prefix_count(conv)
        body = conv[system_count:]
        if len(body) <= 2:
            return 'skipped'

        keep_count = self._keep_recent_count(body, keep_recent_bytes)
        messages_to_compress = body[:-keep_count]
        if not messages_to_compress:
            return 'skipped'

        # 执行压缩
        try:
            use_tools = self.get_config('EnableBuiltinFunctionCalling')
            allow_memory = self.get_config('AllowAccessMemory')

            if use_tools and allow_memory:
                await self._compress_with_memory(
                    conv, messages_to_compress, str(session_id), conversation_dict)
            else:
                await self._compress_with_summary(conv, messages_to_compress)

            # 压缩直接改了内存中的消息列表，显式落盘分离文件
            self._sessions.save(conversation_dict, session_id)
            _log.info(
                f'会话 {conversation_dict}/{session_id} 已压缩：'
                f'压缩 {len(messages_to_compress)} 条，保留最近 {keep_count} 条')
            return 'compressed'

        except Exception as e:
            _log.error(f'压缩 {conversation_dict}/{session_id} 失败（会话历史保持不变）：{e}')
            return 'failed'

    @staticmethod
    def _system_prefix_count(conv: list[dict]) -> int:
        """会话开头 system 提示词的条数（0 或 1），压缩时需原样保留。"""
        return 1 if conv and conv[0].get('role') == 'system' else 0

    @staticmethod
    def _keep_recent_count(body: list[dict], keep_recent_bytes: int) -> int:
        """从尾部往前统计需要原样保留的消息条数（至少 1 条）。"""
        keep_bytes = keep_recent_bytes
        keep_count = 0
        for msg in reversed(body):
            msg_size = len(json.dumps(msg, ensure_ascii=False).encode('utf-8'))
            if keep_bytes >= msg_size:
                keep_bytes -= msg_size
                keep_count += 1
            else:
                break
        return max(1, keep_count)

    def _calculate_conversation_size(self, messages: list[dict], method: str = 'bytes') -> int:
        """计算会话大小。"""
        if method == 'bytes':
            return len(json.dumps(messages, ensure_ascii=False).encode('utf-8'))
        elif method == 'tokens':
            # 粗略估算 token 数
            total = 0
            for msg in messages:
                content = msg.get('content', '')
                if isinstance(content, str):
                    # 简单估算：中文字符×1.5，英文字符×1
                    total += sum(1.5 if ord(c) > 127 else 1 for c in content)
                elif isinstance(content, list):
                    for part in content:
                        if part.get('type') == 'text':
                            text = part.get('text', '')
                            total += sum(1.5 if ord(c) > 127 else 1 for c in text)
            return int(total)
        return 0

    @staticmethod
    def _message_text_for_summary(content) -> str:
        """把单条消息的 content 渲染成摘要用的纯文本。

        图片仅保留 ``[图片]`` 占位，绝不把 base64 送进摘要请求。
        """
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get('type') == 'text':
                    text = str(part.get('text', '')).strip()
                    if text:
                        parts.append(text)
                elif part.get('type') == 'image_url':
                    parts.append(IMAGE_PLACEHOLDER_TEXT)
            return ' '.join(parts).strip()
        return ''

    def _build_summary_input(self, messages: list[dict]) -> str:
        """把待压缩消息拼成摘要请求的输入文本。

        超出 ``SUMMARY_INPUT_BUDGET_CHARS`` 时按**等距抽样**取行（而不是只截取
        开头），保证摘要覆盖整段被删除的历史；抽样比例会写警告日志，便于发现
        阈值配置过大的情况。
        """
        lines: list[str] = []
        for msg in messages:
            text = self._message_text_for_summary(msg.get('content'))
            if not text:
                continue
            if len(text) > SUMMARY_LINE_MAX_CHARS:
                text = text[:SUMMARY_LINE_MAX_CHARS] + '…'
            lines.append(f'{msg.get("role")}: {text}')
        if not lines:
            return ''

        total_chars = sum(len(line) + 1 for line in lines)
        if total_chars <= SUMMARY_INPUT_BUDGET_CHARS:
            return '\n'.join(lines)

        # 等距抽样：按平均行长估算可容纳的行数，再按比例挑行；
        # 末行（最接近保留窗口的最新消息）必须保留，否则摘要会缺掉紧邻的上下文
        avg_line_chars = total_chars / len(lines)
        max_lines = max(8, int(SUMMARY_INPUT_BUDGET_CHARS / max(avg_line_chars, 1)))
        step = len(lines) / max_lines
        picked = [lines[int(i * step)] for i in range(max_lines - 1)]
        picked.append(lines[-1])
        sampled = '\n'.join(picked)[:SUMMARY_INPUT_BUDGET_CHARS]
        _log.warning(
            f'待压缩历史共 {total_chars} 字符，超出摘要预算 {SUMMARY_INPUT_BUDGET_CHARS}，'
            f'已等距抽样 {len(picked)}/{len(lines)} 条消息；'
            '如需更完整的摘要请调低 CompressionThresholdBytes')
        return sampled

    async def _generate_compression_summary(self, to_compress: list[dict]) -> str:
        """调用模型把待压缩历史汇总成摘要（失败向上抛出，由外层统一处理）。"""
        history_text = self._build_summary_input(to_compress)
        response = await asyncio.to_thread(
            lambda: self._default_client.chat.completions.create(
                model=self.get_config('Model'),
                messages=[
                    {'role': 'system', 'content': CONVERSATION_COMPRESSION_SYSTEM_PROMPT},
                    {'role': 'user', 'content': CONVERSATION_COMPRESSION_USER_TEMPLATE.format(
                        history=history_text)},
                ],
                temperature=0.3,
            )
        )
        summary = (response.choices[0].message.content or '').strip()
        if not summary:
            raise RuntimeError('模型返回了空摘要')
        return summary

    @staticmethod
    def _tool_payload_succeeded(raw) -> bool:
        """判断 tools.* 返回的 JSON payload 是否成功。"""
        try:
            payload = json.loads(raw) if isinstance(raw, str) else raw
        except json.JSONDecodeError:
            return False
        return isinstance(payload, dict) and payload.get('status') == 'success'

    async def _compress_with_memory(self, conv: list[dict], to_compress: list[dict],
                                    session_id: str, conv_type: str) -> None:
        """使用记忆工具进行压缩：摘要写入预设 memory.json 后删除被压缩的历史。"""
        summary = await self._generate_compression_summary(to_compress)

        # 存储到记忆
        preset_name = self._get_preset_name(conv_type, session_id)
        from_user = 0
        from_group = -1

        if conv_type == 'group_conversations':
            from_group = int(session_id) if session_id.isdigit() else -1
        else:
            from_user = int(session_id) if session_id.isdigit() else 0

        raw = tools.access_memory(
            os.path.join(str(self.workspace), 'presets', preset_name),
            action='add',
            content=f'[会话压缩摘要]\n{summary}',
            from_user=from_user,
            from_group=from_group,
        )
        # 写入记忆失败必须中止：否则摘要丢了、历史也被删了
        if not self._tool_payload_succeeded(raw):
            raise RuntimeError(f'写入预设记忆失败：{raw}')

        # 移除被压缩的原始消息（保留 system 与最近的尾部消息）
        insert_at = self._system_prefix_count(conv)
        del conv[insert_at:insert_at + len(to_compress)]

    async def _compress_with_summary(self, conv: list[dict], to_compress: list[dict]) -> None:
        """使用摘要进行压缩：摘要作为独立消息插回 system 之后。"""
        summary = await self._generate_compression_summary(to_compress)

        # 从 system 之后删掉被压缩的消息，再把摘要插回去
        insert_at = self._system_prefix_count(conv)
        del conv[insert_at:insert_at + len(to_compress)]
        conv.insert(insert_at, {
            'role': 'user',
            'content': f'[历史摘要] {summary}'
        })


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
