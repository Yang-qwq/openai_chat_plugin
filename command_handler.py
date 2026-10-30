# -*- coding: utf-8 -*-
import re
import shlex
import time

from ncatbot.core import registrar
from ncatbot.event.qq import GroupMessageEvent, MessageEvent
from ncatbot.utils import get_config_manager
from ncatbot.utils.logger import get_log

from . import tools
from .preset_manager import Preset

_log = get_log('openai_chat_plugin')

DEFAULT_PRESET_NAME = 'default'

# 管理员权限点（RBAC，root 在 on_load 时自动授权）
ADMIN_PERMISSION = 'OpenAIChatPlugin.admin'
# 群角色缓存 TTL（秒），防止刷群成员列表 API
GROUP_ROLE_CACHE_TTL = 300

# 匹配 At 组件 CQ 码（如 [CQ:at,qq=888888]），用于解析 /chatrbac 目标
_AT_CQ_PATTERN = re.compile(r'^\[CQ:at,qq=(\d+)\]$', re.IGNORECASE)

ADMIN_HELP_TEXT = '''OpenAI Chat Plugin 管理员命令帮助：

/chat-admin set-preset <name> [group:<id>|user:<id>] - 设置预设（管理员功能）
/chat-admin reset [group:<id>|user:<id>] - 重置会话（管理员功能）
/chat-admin update-prompt [group:<id>|user:<id>|all(default)] - 更新指定用户的提示词，不清除会话记录（管理员功能）
/chat-admin task list - 查看全部定时任务（含所属会话、停用状态与下次执行时间）
/chat-admin task delete <name> - 删除指定定时任务（可跨会话清理）
/chat-admin task pause - 暂停所有定时任务（记录保留，可随时 resume）
/chat-admin task resume - 恢复定时任务执行并重新注册
/chat-admin help - 显示此帮助信息

示例：
/chat-admin set-preset MyPreset
/chat-admin set-preset MyPreset group:1919810
/chat-admin set-preset MyPreset user:114514
/chat-admin reset
/chat-admin reset group:1919810
/chat-admin reset user:114514

注意：这些命令仅限全局管理员（/chatrbac 授权）或本群群主/群管理使用，可以跨群聊设置预设'''

USER_HELP_TEXT = '''OpenAI Chat Plugin 用户命令帮助：

/chat set-preset <name> - 设置预设（仅限当前用户/群组）
/chat reset - 重置当前会话
/chat nolisten [on|off] - 开关本群随机插话（on = 关闭随机插话，off = 开启；不带参数查看当前状态）
/chat help - 显示此帮助信息

示例：
/chat set-preset MyPreset
/chat reset
/chat nolisten on
/chat nolisten
/chat help
'''

CHATRBAC_HELP_TEXT = '''OpenAI Chat Plugin 全局管理员管理命令帮助：

/chatrbac grant <qq> - 授予全局管理员权限（支持 @ 成员或纯数字）
/chatrbac revoke <qq> - 撤销全局管理员权限
/chatrbac list - 查看全局管理员列表
/chatrbac help - 显示此帮助信息

注意：仅限全局管理员（含机器人 owner）使用，群主/群管理身份不可越权调用'''


def _parse_command(event: MessageEvent) -> list | None:
    """解析消息为命令参数列表（shlex），失败时回复格式错误提示

    :param event: 消息事件
    :return: 参数列表；解析失败返回 None（已回复用户）
    """
    replaced_message = event.raw_message.replace('\\n', '\n')
    try:
        return shlex.split(replaced_message)
    except ValueError:
        _log.warning(f'命令解析失败（引号不匹配等）：{event.raw_message[:100]}')
        return None


class OpenAICommandHandlerMixin:
    """命令处理逻辑 Mixin，供 OpenAIChatPlugin 继承使用"""

    # ------------------------------------------------------------------
    # 权限体系（三层：RBAC 全局管理员 > 本群群主/群管理自动放行 > 默认拒绝）
    # ------------------------------------------------------------------

    async def _check_admin(self, event: MessageEvent) -> bool:
        """校验管理员权限：RBAC 全局管理员 或 本群群主/群管理（受开关控制）

        :param event: 消息事件
        :return: 是否拥有权限
        """
        if await self._check_global_admin(event):
            return True
        if await self._is_group_privileged(event):
            return True
        await event.reply(text='权限不足：该命令需要全局管理员权限（/chatrbac 授权）或本群群主/群管理身份',
                          at_sender=False)
        return False

    async def _check_global_admin(self, event: MessageEvent) -> bool:
        """校验全局管理员权限（仅 RBAC，跨群生效）

        用于 /chatrbac 等不允许群主/群管理越权使用的命令。

        :param event: 消息事件
        :return: 是否拥有权限
        """
        if self.check_permission(str(event.user_id), ADMIN_PERMISSION):
            return True
        await event.reply(text='权限不足：该命令需要全局管理员权限（机器人 owner 或 /chatrbac 授权）',
                          at_sender=False)
        return False

    async def _is_group_privileged(self, event: MessageEvent) -> bool:
        """校验是否为本群群主或群管理员（受 EnableGroupOwnerAutoAuth 开关控制）

        群角色列表带 TTL 缓存，避免每条命令都调用群成员列表 API。

        :param event: 消息事件
        :return: 是否拥有本群管理权限
        """
        if not isinstance(event, GroupMessageEvent):
            return False
        if not self.get_config('EnableGroupOwnerAutoAuth', True):
            return False
        group_id = str(event.group_id)
        user_id = str(event.user_id)

        cache = getattr(self, '_group_role_cache', None)
        if cache is None:
            cache = self._group_role_cache = {}
        entry = cache.get(group_id)

        now = time.time()
        if not entry or now > entry['expire']:
            try:
                members = await self.api.qq.query.get_group_member_list(event.group_id)
            except Exception as e:
                _log.warning(f'获取群成员列表失败: {e}')
                return False
            roles = {}
            for member in members:
                uid = getattr(member, 'user_id', None)
                role = getattr(member, 'role', None)
                if uid is not None:
                    roles[str(uid)] = role
            entry = {'roles': roles, 'expire': now + GROUP_ROLE_CACHE_TTL}
            cache[group_id] = entry
            _log.debug(f'已刷新群({group_id})角色缓存，共 {len(roles)} 人')

        return entry['roles'].get(user_id) in ('owner', 'admin')

    @staticmethod
    def _resolve_target_qq(token: str) -> str | None:
        """解析 /chatrbac 目标 QQ，兼容纯数字与 At 组件（CQ 码）两种格式

        :param token: 命令参数中的目标 QQ（如 "888888" 或 "[CQ:at,qq=888888]"）
        :return: 归一化的 QQ 号；无法解析（如 @全体成员）时返回 None
        """
        token = token.strip()
        if token.isdigit():
            return token
        match = _AT_CQ_PATTERN.match(token)
        if match:
            return match.group(1)
        return None

    # ------------------------------------------------------------------
    # /chat-admin 管理员命令
    # ------------------------------------------------------------------

    @registrar.qq.on_command('/chat-admin')
    async def admin_command_handler(self, event: MessageEvent):
        """处理管理员命令事件（首行权限校验：RBAC 或本群群主/群管理）

        :param event: 事件对象
        :return:
        """
        # 防提权：管理员命令必须先经过权限校验，默认拒绝
        if not await self._check_admin(event):
            return

        command = _parse_command(event)
        if command is None:
            await event.reply(text='命令格式错误，请检查引号是否匹配', at_sender=False)
            return

        if len(command) == 1:
            await event.reply(text=ADMIN_HELP_TEXT, at_sender=False)
            return

        elif len(command) > 1:
            if command[1] == 'set-preset':
                if len(command) < 3:
                    await event.reply(text='请提供预设名称', at_sender=False)
                    return

                preset_name = command[2]
                target = None

                if len(command) > 3:
                    target = command[3]

                preset = Preset()
                if not preset.load(str(self.workspace), preset_name):
                    await event.reply(text=f'预设 {preset_name} 不存在', at_sender=False)
                    return

                conversations = preset.to_conversations()
                display_name = preset.get_display_name()

                if target is None:
                    if isinstance(event, GroupMessageEvent):
                        self._sessions.replace('group_conversations', event.group_id, conversations.copy())
                        self._set_preset_name('group_conversations', event.group_id, preset_name)
                    else:
                        self._sessions.replace('user_conversations', event.user_id, conversations.copy())
                        self._set_preset_name('user_conversations', event.user_id, preset_name)

                    await event.reply(text=f'已设置当前预设为: {preset_name}({display_name})', at_sender=False)
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self._sessions.replace('group_conversations', group_id, conversations.copy())
                            self._set_preset_name('group_conversations', group_id, preset_name)
                            await event.reply(
                                text=f'已为群组 {group_id} 设置预设: {preset_name}({display_name})',
                                at_sender=False)
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self._sessions.replace('user_conversations', user_id, conversations.copy())
                            self._set_preset_name('user_conversations', user_id, preset_name)
                            await event.reply(
                                text=f'已为用户 {user_id} 设置预设: {preset_name}({display_name})',
                                at_sender=False)
                        else:
                            await event.reply(text='目标格式错误，请使用 group:<id> 或 user:<id>', at_sender=False)
                    except (ValueError, IndexError):
                        await event.reply(text='目标格式错误，请使用 group:<id> 或 user:<id>', at_sender=False)

            elif command[1] == 'reset':
                target = None

                if len(command) > 2:
                    target = command[2]

                if target is None:
                    if isinstance(event, GroupMessageEvent):
                        conversation_dict = 'group_conversations'
                        session_id = event.group_id
                    else:
                        conversation_dict = 'user_conversations'
                        session_id = event.user_id
                    preset_name = self._get_preset_name(conversation_dict, session_id)
                    preset = Preset()
                    if not preset.load(str(self.workspace), preset_name):
                        await event.reply(text=f'预设 {preset_name} 不存在，无法重置会话', at_sender=False)
                        return
                    self._sessions.replace(conversation_dict, session_id, preset.to_conversations())
                    await event.reply(text='已重置当前会话', at_sender=False)
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            preset_name = self._get_preset_name('group_conversations', group_id)
                            preset = Preset()
                            if not preset.load(str(self.workspace), preset_name):
                                await event.reply(text=f'预设 {preset_name} 不存在，无法重置会话', at_sender=False)
                                return
                            self._sessions.replace('group_conversations', group_id, preset.to_conversations())
                            await event.reply(text=f'已重置群组 {group_id} 的会话', at_sender=False)
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            preset_name = self._get_preset_name('user_conversations', user_id)
                            preset = Preset()
                            if not preset.load(str(self.workspace), preset_name):
                                await event.reply(text=f'预设 {preset_name} 不存在，无法重置会话', at_sender=False)
                                return
                            self._sessions.replace('user_conversations', user_id, preset.to_conversations())
                            await event.reply(text=f'已重置用户 {user_id} 的会话', at_sender=False)
                        else:
                            await event.reply(text='目标格式错误，请使用 group:<id> 或 user:<id>', at_sender=False)
                    except (ValueError, IndexError):
                        await event.reply(text='目标格式错误，请使用 group:<id> 或 user:<id>', at_sender=False)

            elif command[1] == 'update-prompt':
                _log.info('正在批量更新所有会话的提示词...')
                target = None
                if len(command) > 2:
                    target = command[2]

                if target is None or target.lower() == 'all':
                    updated = 0
                    for group_id in self._sessions.ids('group_conversations'):
                        if self._refresh_system_prompt_in_session('group_conversations', group_id):
                            updated += 1
                    for user_id in self._sessions.ids('user_conversations'):
                        if self._refresh_system_prompt_in_session('user_conversations', user_id):
                            updated += 1
                    _log.info(f'已批量更新提示词，成功处理 {updated} 个会话')
                    await event.reply(text=f'已批量更新提示词，成功处理 {updated} 个会话', at_sender=False)
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            if not self._sessions.has('group_conversations', group_id):
                                _log.warning(f'群组 {group_id} 暂无会话记录，无法更新提示词')
                                await event.reply(text=f'群组 {group_id} 暂无会话记录', at_sender=False)
                                return
                            if self._refresh_system_prompt_in_session('group_conversations', group_id):
                                _log.info(f'已更新群组 {group_id} 的提示词')
                                await event.reply(text=f'已更新群组 {group_id} 的提示词', at_sender=False)
                            else:
                                _log.error(f'未能更新群组 {group_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                                await event.reply(
                                    text=f'未能更新群组 {group_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）',
                                    at_sender=False)
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            if not self._sessions.has('user_conversations', user_id):
                                _log.warning(f'用户 {user_id} 暂无会话记录，无法更新提示词')
                                await event.reply(text=f'用户 {user_id} 暂无会话记录', at_sender=False)
                                return
                            if self._refresh_system_prompt_in_session('user_conversations', user_id):
                                _log.info(f'已更新用户 {user_id} 的提示词')
                                await event.reply(text=f'已更新用户 {user_id} 的提示词', at_sender=False)
                            else:
                                _log.error(f'未能更新用户 {user_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                                await event.reply(
                                    text=f'未能更新用户 {user_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）',
                                    at_sender=False)
                        else:
                            await event.reply(text='目标格式错误，请使用 group:<id>、user:<id> 或 all', at_sender=False)
                    except (ValueError, IndexError):
                        await event.reply(text='目标格式错误，请使用 group:<id>、user:<id> 或 all', at_sender=False)

            elif command[1] == 'task':
                await self._handle_task_admin(event, command)

            elif command[1] == 'help':
                await event.reply(text=ADMIN_HELP_TEXT, at_sender=False)
                return

            else:
                await event.reply(text='未知管理员命令，请使用 /chat-admin help 查看帮助信息', at_sender=False)

            # 管理操作后持久化数据
            self._save_data()

    async def _handle_task_admin(self, event: MessageEvent, command: list) -> None:
        """处理 /chat-admin task 子命令（list / delete / pause / resume）

        定时任务默认对普通用户只读（list 全局可见），运维侧需要能跨会话清理
        与一键停跑，因此这里的操作不受任务归属限制，但整条命令仍经过
        ``_check_admin()`` 校验。

        :param event: 消息事件
        :param command: 已解析的命令参数（command[2] 为子命令）
        """
        # 定时任务是全局资源：查看他人任务、跨会话删除、全局暂停都属于跨群操作，
        # 必须走严格 RBAC，避免群主/群管理借本群身份提权
        if not await self._check_global_admin(event):
            return

        if len(command) < 3:
            await event.reply(
                text='用法：/chat-admin task list|delete <name>|pause|resume',
                at_sender=False)
            return

        sub_command = command[2].lower()
        tasks = self.data['data'].setdefault('scheduled_tasks', {})

        if sub_command == 'list':
            if not tasks:
                await event.reply(text='当前没有定时任务', at_sender=False)
                return
            lines = ['定时任务列表：']
            for index, (name, info) in enumerate(tasks.items()):
                if index >= 20:
                    lines.append(f'...（共 {len(tasks)} 个，仅显示前 20 个）')
                    break
                if not isinstance(info, dict):
                    lines.append(f'- {name} | 记录已损坏')
                    continue
                try:
                    status = self.get_task_status(tools._scheduled_task_key(name)) or {}
                except Exception as e:
                    _log.debug(f'读取定时任务 {name} 状态失败: {e}')
                    status = {}
                next_run = status.get('next_run')
                if hasattr(next_run, 'isoformat'):
                    next_run = next_run.isoformat()
                if info.get('disabled'):
                    state = '已停用'
                elif status:
                    state = '运行中'
                else:
                    state = '未注册'
                lines.append(
                    f'- {name} | {info.get("interval", "?")} | '
                    f'{info.get("session_type", "?")}:{info.get("session_id", "?")} | '
                    f'{state} | 连续失败={info.get("failures", 0)} | 下次={next_run or "-"}')
            await event.reply(text='\n'.join(lines), at_sender=False)
            return

        if sub_command == 'delete':
            task_name = command[3] if len(command) > 3 else ''
            if not task_name:
                await event.reply(text='请提供要删除的任务名', at_sender=False)
                return
            record = tasks.pop(task_name, None)
            removed = self.remove_scheduled_task(tools._scheduled_task_key(task_name))
            self._save_data()
            if record is None and not removed:
                await event.reply(text=f'任务 {task_name} 不存在', at_sender=False)
                return
            _log.info(f'管理员删除定时任务: name={task_name}')
            await event.reply(text=f'已删除定时任务 {task_name}', at_sender=False)
            return

        if sub_command == 'pause':
            self.set_config('EnableScheduledTaskExecution', False)
            removed_count = 0
            for task_name in list(tasks):
                if self.remove_scheduled_task(tools._scheduled_task_key(task_name)):
                    removed_count += 1
            _log.warning(f'管理员暂停定时任务执行，已移出调度器 {removed_count} 个任务')
            await event.reply(
                text=f'已暂停定时任务执行，移出调度器 {removed_count} 个任务'
                     f'（记录保留，可用 /chat-admin task resume 恢复）',
                at_sender=False)
            return

        if sub_command == 'resume':
            self.set_config('EnableScheduledTaskExecution', True)
            restored = self._restore_scheduled_tasks()
            _log.info(f'管理员恢复定时任务执行，重新注册 {restored} 个任务')
            await event.reply(
                text=f'已恢复定时任务执行，重新注册 {restored} 个任务'
                     f'（已停用的任务需先删除后重建）',
                at_sender=False)
            return

        await event.reply(
            text='未知 task 子命令，请使用 /chat-admin task list|delete <name>|pause|resume',
            at_sender=False)

    # ------------------------------------------------------------------
    # /chat 用户命令
    # ------------------------------------------------------------------

    @registrar.qq.on_command('/chat')
    async def user_command_handler(self, event: MessageEvent):
        """处理用户命令事件

        :param event: 事件对象
        :return:
        """
        command = _parse_command(event)
        if command is None:
            await event.reply(text='命令格式错误，请检查引号是否匹配', at_sender=False)
            return

        if len(command) == 1:
            await event.reply(text=USER_HELP_TEXT, at_sender=False)
            return

        elif len(command) > 1:
            if isinstance(event, GroupMessageEvent):
                conversation_dict = 'group_conversations'
            else:
                conversation_dict = 'user_conversations'

            if command[1] == 'set-preset':
                if len(command) < 3:
                    await event.reply(text='请提供预设名称', at_sender=False)
                    return

                preset_name = command[2]

                preset = Preset()
                if not preset.load(str(self.workspace), preset_name):
                    await event.reply(text=f'预设 {preset_name} 不存在', at_sender=False)
                    return

                conversations = preset.to_conversations()
                display_name = preset.get_display_name()
                session_id = event.group_id if isinstance(event, GroupMessageEvent) else event.user_id
                self._sessions.replace(conversation_dict, session_id, conversations.copy())
                self._set_preset_name(conversation_dict, session_id, preset_name)
                await event.reply(text=f'已设置当前预设为: {preset_name}({display_name})', at_sender=False)

                # 设置预设后持久化数据（preset_names 存 data.json，会话文件已由 replace 落盘）
                self._save_data()

            elif command[1] == 'reset':
                session_id = event.group_id if isinstance(event, GroupMessageEvent) else event.user_id
                preset_name = self._get_preset_name(conversation_dict, session_id)
                preset = Preset()
                if not preset.load(str(self.workspace), preset_name):
                    await event.reply(text=f'预设 {preset_name} 不存在，无法重置会话', at_sender=False)
                    return

                self._sessions.replace(conversation_dict, session_id, preset.to_conversations())
                await event.reply(text='已重置当前会话', at_sender=False)
                # 重置会话后持久化数据（会话文件已由 replace 落盘，此处保存 data.json）
                self._save_data()
                return

            elif command[1] == 'nolisten':  # 群级随机插话开关
                if not isinstance(event, GroupMessageEvent):
                    await event.reply(text='nolisten 命令仅在群聊中有效', at_sender=False)
                    return
                group_nolisten = self.data['data'].setdefault('group_nolisten', {})
                if len(command) < 3:
                    state = '已关闭' if group_nolisten.get(event.group_id, False) else '已开启'
                    await event.reply(
                        text=f'本群随机插话当前{state}（用法：/chat nolisten on|off）', at_sender=False)
                    return
                param = command[2].lower()
                if param not in ('on', 'off'):
                    await event.reply(text='nolisten 参数必须为 on 或 off', at_sender=False)
                    return
                # nolisten on = 本群不再随机插话
                group_nolisten[event.group_id] = (param == 'on')
                state = '关闭' if param == 'on' else '开启'
                await event.reply(text=f'已{state}本群（{event.group_id}）的随机插话', at_sender=False)
                # 配置变更后持久化数据
                self._save_data()

            elif command[1] == 'help':
                await event.reply(text=USER_HELP_TEXT, at_sender=False)

            else:
                await event.reply(text='未知命令，请使用 /chat help 查看帮助信息', at_sender=False)

    # ------------------------------------------------------------------
    # /chatrbac 全局管理员管理命令（严格 RBAC，防群主/群管理提权）
    # ------------------------------------------------------------------

    @registrar.qq.on_command('/chatrbac')
    async def on_rbac(self, event: MessageEvent):
        """处理 /chatrbac 命令，管理全局管理员权限（仅全局管理员/root 可用）

        :param event: 消息事件
        :return: None
        """
        # 防提权：授予/撤销全局权限仅走严格 RBAC 校验，群主/群管理不可越权
        if not await self._check_global_admin(event):
            return

        command = _parse_command(event)
        params = command[1:] if command and len(command) > 1 else []
        if not params or params[0] == 'help':
            await event.reply(text=CHATRBAC_HELP_TEXT, at_sender=False)
            return

        subcommand = params[0]

        if subcommand == 'grant':
            if len(params) != 2:
                await event.reply(text='用法：/chatrbac grant <qq>（支持 @ 成员或纯数字）', at_sender=False)
                return
            target = self._resolve_target_qq(params[1])
            if target is None:
                await event.reply(text='QQ 号必须是纯数字或 @ 成员', at_sender=False)
                return
            _log.debug(f'{event.user_id} 通过 /chatrbac grant 指定目标: {target}（原文: {params[1]}）')
            if not self.rbac:
                await event.reply(text='RBAC 服务不可用，无法授权', at_sender=False)
                return
            if self.check_permission(target, ADMIN_PERMISSION):
                await event.reply(text=f'用户 {target} 已是全局管理员', at_sender=False)
                return
            try:
                self.rbac.add_user(target, exist_ok=True)
                self.rbac.grant('user', target, ADMIN_PERMISSION)
            except Exception as e:
                _log.error(f'授予全局管理员权限失败: {e}')
                await event.reply(text=f'授予全局管理员权限失败: {e}', at_sender=False)
                return
            _log.info(f'{event.user_id} 通过 /chatrbac 授予 {target} 全局管理员权限')
            await event.reply(text=f'✅ 已授予 {target} 全局管理员权限', at_sender=False)

        elif subcommand == 'revoke':
            if len(params) != 2:
                await event.reply(text='用法：/chatrbac revoke <qq>（支持 @ 成员或纯数字）', at_sender=False)
                return
            target = self._resolve_target_qq(params[1])
            if target is None:
                await event.reply(text='QQ 号必须是纯数字或 @ 成员', at_sender=False)
                return
            _log.debug(f'{event.user_id} 通过 /chatrbac revoke 指定目标: {target}（原文: {params[1]}）')
            if not self.rbac:
                await event.reply(text='RBAC 服务不可用，无法撤销', at_sender=False)
                return
            if not self.check_permission(target, ADMIN_PERMISSION):
                await event.reply(text=f'用户 {target} 不是全局管理员', at_sender=False)
                return
            try:
                self.rbac.revoke('user', target, ADMIN_PERMISSION)
            except Exception as e:
                _log.error(f'撤销全局管理员权限失败: {e}')
                await event.reply(text=f'撤销全局管理员权限失败: {e}', at_sender=False)
                return
            _log.info(f'{event.user_id} 通过 /chatrbac 撤销 {target} 全局管理员权限')
            await event.reply(text=f'✅ 已撤销 {target} 的全局管理员权限', at_sender=False)

        elif subcommand == 'list':
            if not self.rbac:
                await event.reply(text='RBAC 服务不可用', at_sender=False)
                return
            root = get_config_manager().config.root
            admins = [
                uid for uid in self.rbac.users
                if self.check_permission(uid, ADMIN_PERMISSION)
            ]
            if not admins:
                await event.reply(
                    text='当前暂无全局管理员（root 将在下次启动时自动恢复授权）',
                    at_sender=False)
                return
            lines = [f'🔐 全局管理员列表（共 {len(admins)} 人）：', '']
            for i, uid in enumerate(admins, 1):
                root_mark = '（root）' if uid == root else ''
                lines.append(f'  {i}. {uid}{root_mark}')
            await event.reply(text='\n'.join(lines), at_sender=False)

        else:
            await event.reply(text=CHATRBAC_HELP_TEXT, at_sender=False)

    # ------------------------------------------------------------------
    # 会话/预设名辅助方法
    # ------------------------------------------------------------------

    def _get_preset_name(self, conversation_dict: str, session_id) -> str:
        """获取会话当前使用的预设名称

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID（统一按 str 查表，与 SessionStore / data.json 一致）
        :return: 预设名称，若无记录则返回默认预设
        """
        key = 'group_preset_names' if conversation_dict == 'group_conversations' else 'user_preset_names'
        names = self.data['data'][key]
        session_key = str(session_id)
        # 统一按 str 查表；同时兼容历史遗留的 int 键（旧版 /chat-admin set-preset 曾用 int 写入）
        preset_name = names.get(session_key) or names.get(session_id)
        return preset_name or DEFAULT_PRESET_NAME

    def _set_preset_name(self, conversation_dict: str, session_id, preset_name: str) -> None:
        """记录会话使用的预设名称

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID（统一存为 str 键，避免 int/str 两份记录）
        :param preset_name: 预设名称
        """
        key = 'group_preset_names' if conversation_dict == 'group_conversations' else 'user_preset_names'
        names = self.data['data'][key]
        session_key = str(session_id)
        # 清掉可能存在的 int 旧键，避免同一会话出现两份预设记录
        if not isinstance(session_id, str):
            names.pop(session_id, None)
        names[session_key] = preset_name

    def _refresh_system_prompt_in_session(self, conversation_dict: str, session_id) -> bool:
        """从磁盘预设更新会话中的 system 提示词，保留 user / assistant 等其余消息

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID
        :return: 是否成功更新
        """
        conversations = self._sessions.load(conversation_dict, session_id)
        if conversations is None:
            return False
        preset_name = self._get_preset_name(conversation_dict, session_id)
        preset = Preset()
        if not preset.load(str(self.workspace), preset_name):
            _log.error(f'预设 {preset_name} 不存在，无法更新 {conversation_dict} {session_id} 的提示词')
            return False
        preset_template = preset.to_conversations()
        if not preset_template or preset_template[0]['role'] != 'system':
            _log.warning(f'预设 {preset_name} 没有有效的 system 消息，跳过 {conversation_dict} {session_id}')
            return False
        new_system = {'role': 'system', 'content': preset_template[0]['content']}
        if len(conversations) > 0 and conversations[0]['role'] == 'system':
            conversations[0] = new_system
        else:
            conversations.insert(0, new_system)
        # 消息列表已原地修改，显式落盘分离文件
        self._sessions.save(conversation_dict, session_id)
        _log.info(f'已更新 {conversation_dict} 中 {session_id} 的提示词')
        return True
