# -*- coding: utf-8 -*-
import shlex

from ncatbot.core import BaseMessage, GroupMessage, PrivateMessage
from ncatbot.utils.logger import get_log

from .present_manager import get_preset_display_name, load_preset

_log = get_log('openai_chat_plugin')

DEFAULT_PRESENT_NAME = 'default'

ADMIN_HELP_TEXT = '''OpenAI Chat Plugin 管理员命令帮助：

/chat-admin set-present <name> [group:<id>|user:<id>] - 设置预设（管理员功能）
/chat-admin reset [group:<id>|user:<id>] - 重置会话（管理员功能）
/chat-admin update-prompt [group:<id>|user:<id>|all(default)] - 更新指定用户的提示词，不清除会话记录（管理员功能）
/chat-admin help - 显示此帮助信息

示例：
/chat-admin set-present MyPresent
/chat-admin set-present MyPresent group:1919810
/chat-admin set-present MyPresent user:114514
/chat-admin reset
/chat-admin reset group:1919810
/chat-admin reset user:114514

注意：这些命令仅限管理员使用，可以跨群聊设置预设'''

USER_HELP_TEXT = '''OpenAI Chat Plugin 用户命令帮助：

/chat set-present <name> - 设置预设（仅限当前用户/群组）
/chat reset - 重置当前会话
/chat help - 显示此帮助信息

示例：
/chat set-present MyPresent
/chat reset
/chat help
'''


class OpenAICommandHandlerMixin:
    """命令处理逻辑 Mixin，供 OpenAIChatPlugin 继承使用"""

    async def admin_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理管理员命令事件

        :param event: 事件对象
        :return:
        """
        replaced_message = event.raw_message.replace('\\n', '\n')

        command = shlex.split(replaced_message)

        if command[0] != '/chat-admin':
            return

        if len(command) == 1:
            await event.reply_text(ADMIN_HELP_TEXT)
            return

        elif len(command) > 1:
            if command[1] == 'set-present':
                if len(command) < 3:
                    await event.reply_text('请提供预设名称')
                    return

                present_name = command[2]
                target = None

                if len(command) > 3:
                    target = command[3]

                if target is None:
                    conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                    if conversations is None:
                        await event.reply_text(f'预设 {present_name} 不存在')
                        return

                    display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)

                    if event.message_type == 'group':
                        self.data['data']['group_conversations'][event.group_id] = conversations.copy()
                        self._set_preset_name('group_conversations', event.group_id, present_name)
                    else:
                        self.data['data']['user_conversations'][event.user_id] = conversations.copy()
                        self._set_preset_name('user_conversations', event.user_id, present_name)

                    await event.reply_text(f'已设置当前预设为: {present_name}({display_name})')
                else:
                    conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                    if conversations is None:
                        await event.reply_text(f'预设 {present_name} 不存在')
                        return

                    display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)

                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            self.data['data']['group_conversations'][group_id] = conversations.copy()
                            self._set_preset_name('group_conversations', group_id, present_name)
                            await event.reply_text(f'已为群组 {group_id} 设置预设: {present_name}({display_name})')
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            self.data['data']['user_conversations'][user_id] = conversations.copy()
                            self._set_preset_name('user_conversations', user_id, present_name)
                            await event.reply_text(f'已为用户 {user_id} 设置预设: {present_name}({display_name})')
                        else:
                            await event.reply_text('目标格式错误，请使用 group:<id> 或 user:<id>')
                    except (ValueError, IndexError):
                        await event.reply_text('目标格式错误，请使用 group:<id> 或 user:<id>')

            elif command[1] == 'reset':
                target = None

                if len(command) > 2:
                    target = command[2]

                if target is None:
                    if event.message_type == 'group':
                        preset_name = self._get_preset_name('group_conversations', event.group_id)
                        preset_conversations = load_preset(self.work_space.path.as_posix() + '/', preset_name)
                        if preset_conversations is None:
                            await event.reply_text(f'预设 {preset_name} 不存在，无法重置会话')
                            return
                        self.data['data']['group_conversations'][event.group_id] = preset_conversations.copy()
                    else:
                        preset_name = self._get_preset_name('user_conversations', event.user_id)
                        preset_conversations = load_preset(self.work_space.path.as_posix() + '/', preset_name)
                        if preset_conversations is None:
                            await event.reply_text(f'预设 {preset_name} 不存在，无法重置会话')
                            return
                        self.data['data']['user_conversations'][event.user_id] = preset_conversations.copy()
                    await event.reply_text('已重置当前会话')
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            preset_name = self._get_preset_name('group_conversations', group_id)
                            preset_conversations = load_preset(self.work_space.path.as_posix() + '/', preset_name)
                            if preset_conversations is None:
                                await event.reply_text(f'预设 {preset_name} 不存在，无法重置会话')
                                return
                            self.data['data']['group_conversations'][group_id] = preset_conversations.copy()
                            await event.reply_text(f'已重置群组 {group_id} 的会话')
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            preset_name = self._get_preset_name('user_conversations', user_id)
                            preset_conversations = load_preset(self.work_space.path.as_posix() + '/', preset_name)
                            if preset_conversations is None:
                                await event.reply_text(f'预设 {preset_name} 不存在，无法重置会话')
                                return
                            self.data['data']['user_conversations'][user_id] = preset_conversations.copy()
                            await event.reply_text(f'已重置用户 {user_id} 的会话')
                        else:
                            await event.reply_text('目标格式错误，请使用 group:<id> 或 user:<id>')
                    except (ValueError, IndexError):
                        await event.reply_text('目标格式错误，请使用 group:<id> 或 user:<id>')

            elif command[1] == 'update-prompt':
                _log.info('正在批量更新所有会话的提示词...')
                target = None
                if len(command) > 2:
                    target = command[2]

                if target is None or target.lower() == 'all':
                    updated = 0
                    for group_id in self.data['data']['group_conversations']:
                        if self._refresh_system_prompt_in_session('group_conversations', group_id):
                            updated += 1
                    for user_id in self.data['data']['user_conversations']:
                        if self._refresh_system_prompt_in_session('user_conversations', user_id):
                            updated += 1
                    _log.info(f'已批量更新提示词，成功处理 {updated} 个会话')
                    await event.reply_text(f'已批量更新提示词，成功处理 {updated} 个会话')
                else:
                    try:
                        if target.startswith('group:'):
                            group_id = int(target.split(':')[1])
                            if group_id not in self.data['data']['group_conversations']:
                                _log.warning(f'群组 {group_id} 暂无会话记录，无法更新提示词')
                                await event.reply_text(f'群组 {group_id} 暂无会话记录')
                                return
                            if self._refresh_system_prompt_in_session('group_conversations', group_id):
                                _log.info(f'已更新群组 {group_id} 的提示词')
                                await event.reply_text(f'已更新群组 {group_id} 的提示词')
                            else:
                                _log.error(f'未能更新群组 {group_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                                await event.reply_text(
                                    f'未能更新群组 {group_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                        elif target.startswith('user:'):
                            user_id = int(target.split(':')[1])
                            if user_id not in self.data['data']['user_conversations']:
                                _log.warning(f'用户 {user_id} 暂无会话记录，无法更新提示词')
                                await event.reply_text(f'用户 {user_id} 暂无会话记录')
                                return
                            if self._refresh_system_prompt_in_session('user_conversations', user_id):
                                _log.info(f'已更新用户 {user_id} 的提示词')
                                await event.reply_text(f'已更新用户 {user_id} 的提示词')
                            else:
                                _log.error(f'未能更新用户 {user_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                                await event.reply_text(
                                    f'未能更新用户 {user_id} 的提示词（预设不存在、无有效 system 或 prompt 为空）')
                        else:
                            await event.reply_text('目标格式错误，请使用 group:<id>、user:<id> 或 all')
                    except (ValueError, IndexError):
                        await event.reply_text('目标格式错误，请使用 group:<id>、user:<id> 或 all')

            elif command[1] == 'help':
                await event.reply_text(ADMIN_HELP_TEXT)
                return

            else:
                await event.reply_text('未知管理员命令，请使用 /chat-admin help 查看帮助信息')

    async def user_command_handler(self, event: BaseMessage | GroupMessage | PrivateMessage):
        """处理用户命令事件

        :param event: 事件对象
        :return:
        """

        replaced_message = event.raw_message.replace('\\n', '\n')

        command = shlex.split(replaced_message)

        if command[0] != '/chat':
            return

        if len(command) == 1:
            await event.reply_text(USER_HELP_TEXT)
            return

        elif len(command) > 1:
            if event.message_type == 'group':
                conversation_dict = 'group_conversations'
            else:
                conversation_dict = 'user_conversations'

            if command[1] == 'set-present':
                if len(command) < 3:
                    await event.reply_text('请提供预设名称')
                    return

                present_name = command[2]

                conversations = load_preset(self.work_space.path.as_posix() + '/', present_name)
                if conversations is None:
                    await event.reply_text(f'预设 {present_name} 不存在')
                    return

                display_name = get_preset_display_name(self.work_space.path.as_posix() + '/', present_name)
                session_id = event.group_id if event.message_type == 'group' else event.user_id
                self.data['data'][conversation_dict][session_id] = conversations.copy()
                self._set_preset_name(conversation_dict, session_id, present_name)
                await event.reply_text(f'已设置当前预设为: {present_name}({display_name})')

            elif command[1] == 'reset':
                session_id = event.group_id if event.message_type == 'group' else event.user_id
                preset_name = self._get_preset_name(conversation_dict, session_id)
                preset_conversations = load_preset(self.work_space.path.as_posix() + '/', preset_name)
                if preset_conversations is None:
                    await event.reply_text(f'预设 {preset_name} 不存在，无法重置会话')
                    return

                self.data['data'][conversation_dict][session_id] = preset_conversations.copy()
                await event.reply_text('已重置当前会话')
                return

            elif command[1] == 'help':
                await event.reply_text(USER_HELP_TEXT)

            else:
                await event.reply_text('未知命令，请使用 /chat help 查看帮助信息')

    def _get_preset_name(self, conversation_dict: str, session_id: int) -> str:
        """获取会话当前使用的预设名称

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID
        :return: 预设名称，若无记录则返回默认预设
        """
        key = 'group_preset_names' if conversation_dict == 'group_conversations' else 'user_preset_names'
        return self.data['data'][key].get(session_id, DEFAULT_PRESENT_NAME)

    def _set_preset_name(self, conversation_dict: str, session_id: int, preset_name: str) -> None:
        """记录会话使用的预设名称

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID
        :param preset_name: 预设名称
        """
        key = 'group_preset_names' if conversation_dict == 'group_conversations' else 'user_preset_names'
        self.data['data'][key][session_id] = preset_name

    def _refresh_system_prompt_in_session(self, conversation_dict: str, session_id: int) -> bool:
        """从磁盘预设更新会话中的 system 提示词，保留 user / assistant 等其余消息

        :param conversation_dict: 'group_conversations' 或 'user_conversations'
        :param session_id: 群组ID或用户ID
        :return: 是否成功更新
        """
        conversations = self.data['data'][conversation_dict].get(session_id)
        if conversations is None:
            return False
        preset_name = self._get_preset_name(conversation_dict, session_id)
        preset_template = load_preset(self.work_space.path.as_posix() + '/', preset_name)
        if preset_template is None:
            _log.error(f'预设 {preset_name} 不存在，无法更新 {conversation_dict} {session_id} 的提示词')
            return False
        if len(preset_template) == 0 or preset_template[0]['role'] != 'system':
            _log.warning(f'预设 {preset_name} 没有有效的 system 消息，跳过 {conversation_dict} {session_id}')
            return False
        new_system = {'role': 'system', 'content': preset_template[0]['content']}
        if len(conversations) > 0 and conversations[0]['role'] == 'system':
            conversations[0] = new_system
        else:
            conversations.insert(0, new_system)
        _log.info(f'已更新 {conversation_dict} 中 {session_id} 的提示词')
        return True
