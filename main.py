# -*- coding: utf-8 -*-
import shlex

from ncatbot.core import BaseMessage, GroupMessage, PrivateMessage
from ncatbot.plugin import BasePlugin, CompatibleEnrollment
from ncatbot.utils import config
from ncatbot.utils.logger import get_log
from openai import OpenAI
from pathlib import Path
import re

bot = CompatibleEnrollment  # 兼容回调函数注册器
_log = get_log("openai_chat_plugin")  # 日志记录器


class OpenAIChatPlugin(BasePlugin):
    name = "OpenAIChatPlugin"  # 插件名
    version = "0.0.6"  # 插件版本

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

    def _extract_memory_block(self, text) -> str | None:
        """提取 memory 类型的代码块内容，如果存在则返回内容，否则返回 None"""
        # 检查是否允许访问记忆
        if not self.config.get('allow_access_memory', True):
            return None
            
        pattern = r"```memory\\s*([\s\S]*?)```"
        match = re.search(pattern, text)
        if match:
            return match.group(1).strip()
        return None

    def _get_memory_file_path(self, present_name) -> Path:
        """获取当前 present 的 memory 文件路径"""
        return self._data_path.parent / f"memory_{present_name}.txt"

    def _memory_read(self, file_path: Path) -> str:
        """读取指定 memory 文件内容

        :param file_path: memory 文件路径
        :return: 文件内容字符串，若文件为空则返回提示
        """
        try:
            if not file_path.exists():
                return "(memory 文件不存在)"
            
            # 检查文件大小，防止读取过大的文件
            # file_size = file_path.stat().st_size
            # if file_size > 1024 * 1024:  # 1MB 限制
            #     _log.warning(f"Memory file too large: {file_path}, size: {file_size} bytes")
            #     return f"(memory 文件过大：{file_size // 1024}KB，超过 1MB 限制)"
            
            lines = file_path.read_text(encoding='utf-8').splitlines()
            
            if not lines:
                return "(memory 文件为空)"
            
            content = '\n'.join(lines)
            return content
                
        except UnicodeDecodeError:
            _log.error(f"Unicode decode error when reading memory file: {file_path}")
            return "(读取失败：文件编码错误)"
        except PermissionError:
            _log.error(f"Permission denied when reading memory file: {file_path}")
            return "(读取失败：权限不足)"
        except OSError as e:
            _log.error(f"OS error when reading memory file: {file_path}, error: {e}")
            return f"(读取失败：系统错误 - {e})"
        except Exception as e:
            _log.error(f"Unexpected error when reading memory file: {file_path}, error: {e}")
            return f"(读取失败：未知错误 - {e})"

    def _memory_write(self, file_path: Path, content: str) -> str:
        """向指定 memory 文件追加写入内容

        :param file_path: memory 文件路径
        :param content: 要写入的内容
        :return: 写入结果提示
        """
        try:
            # 验证输入内容
            if not content or not content.strip():
                return "(write 命令内容为空)"
            
            # 限制内容长度，防止过长的内容
            # if len(content) > 10000:  # 10KB 限制
            #     return "(write 命令内容过长，超过 10KB 限制)"
            
            # 确保目录存在
            file_path.parent.mkdir(parents=True, exist_ok=True)
            
            with file_path.open('a', encoding='utf-8') as f:
                f.write(content.strip() + '\n')
            
            _log.info(f"Successfully wrote to memory file: {file_path}, content length: {len(content)}")
            return "(memory 已写入)"
            
        except PermissionError:
            _log.error(f"Permission denied when writing to memory file: {file_path}")
            return "(写入失败：权限不足)"
        except OSError as e:
            _log.error(f"OS error when writing to memory file: {file_path}, error: {e}")
            return f"(写入失败：系统错误 - {e})"
        except Exception as e:
            _log.error(f"Unexpected error when writing to memory file: {file_path}, error: {e}")
            return f"(写入失败：未知错误 - {e})"

    def _memory_regex_search(self, file_path: Path, pattern: str) -> str:
        """在指定 memory 文件中使用正则表达式查找匹配内容

        :param file_path: memory 文件路径
        :param pattern: 正则表达式
        :return: 匹配到的内容或提示信息
        """
        try:
            # 验证正则表达式模式
            if not pattern or not pattern.strip():
                return "(regex_search 命令缺少搜索模式)"
            
            # 限制正则表达式长度，防止过长的模式
            # if len(pattern) > 1000:  # 1KB 限制
            #     return "(regex_search 命令模式过长，超过 1KB 限制)"
            
            # 测试正则表达式是否有效
            try:
                re.compile(pattern)
            except re.error as e:
                return f"(正则表达式语法错误: {e})"
            
            if not file_path.exists():
                return "(memory 文件为空)"
                
            lines = file_path.read_text(encoding='utf-8').splitlines()
            
            # 执行搜索
            matched = []
            for i, line in enumerate(lines):
                try:
                    if re.search(pattern, line):
                        matched.append(f"[{i}] {line}")
                except re.error as e:
                    _log.warning(f"正则表达搜索错误: {e}，在行 {i} 中的内容: {line[:100]}...")
                    continue
            
            if matched:
                _log.debug(f"正则表达式搜索成功，匹配到 {len(matched)} 行")
                return '\n'.join(matched)
            else:
                return "(未找到匹配内容)"
                
        except UnicodeDecodeError:
            _log.error(f"Unicode decode error when reading memory file: {file_path}")
            return "(读取失败：文件编码错误)"
        except Exception as e:
            _log.error(f"Unexpected error in regex search: {e}")
            return f"(搜索失败：未知错误 - {e})"

    def _memory_delete(self, file_path: Path, idx: int) -> str:
        """删除指定 memory 文件中指定索引的内容行

        :param file_path: memory 文件路径
        :param idx: 要删除的行索引（从0开始）
        :return: 删除结果提示
        """
        try:
            # 验证索引
            if idx < 0:
                return f"(删除索引无效：{idx}，索引必须为非负整数)"
            
            if not file_path.exists():
                return "(memory 文件为空)"
            
            lines = file_path.read_text(encoding='utf-8').splitlines()
            
            if idx >= len(lines):
                return f"(索引超出范围：{idx}，文件只有 {len(lines)} 行)"
            
            # 备份要删除的内容
            deleted = lines[idx]
            
            # 删除指定行
            lines.pop(idx)
            
            # 写回文件
            try:
                file_path.write_text('\n'.join(lines) + ('\n' if lines else ''), encoding='utf-8')
                _log.info(f"Successfully deleted line {idx} from memory file: {file_path}, deleted content: {deleted[:100]}...")
                return f"(已删除第{idx}行: {deleted[:100]}{'...' if len(deleted) > 100 else ''})"
            except (PermissionError, OSError) as e:
                _log.error(f"Failed to write back memory file after deletion: {file_path}, error: {e}")
                return f"(删除成功但保存失败：{e})"
                
        except UnicodeDecodeError:
            _log.error(f"Unicode decode error when reading memory file: {file_path}")
            return "(读取失败：文件编码错误)"
        except Exception as e:
            _log.error(f"Unexpected error in delete operation: {e}")
            return f"(删除失败：未知错误 - {e})"

    def _handle_memory_block(self, memory_block, present_name):
        """处理 memory 代码块，支持多条语句和错误处理
        
        :param memory_block: memory 代码块内容
        :param present_name: 当前 present 名称
        :return: 处理结果字符串
        """
        file_path = self._get_memory_file_path(present_name)
        
        # 清理和验证输入
        if not memory_block or not memory_block.strip():
            # _log.warning(f"Empty memory block for present: {present_name}")
            return "(memory 代码块为空)"
        
        # 分割多条语句（按换行符分割）
        commands = [cmd.strip() for cmd in memory_block.split('\n') if cmd.strip()]
        
        if not commands:
            _log.warning(f"memory代码框中没有有效命令，present: {present_name}")
            return "(memory 代码块中没有有效命令)"
        
        # _log.info(f"Processing {len(commands)} memory commands for present: {present_name}")
        
        results = []
        for i, cmd in enumerate(commands):
            try:
                result = self._execute_single_memory_command(cmd, file_path, i + 1)
                results.append(f"[{i + 1}] {result}")
            except Exception as e:
                error_msg = f"[{i + 1}] 命令执行失败: {str(e)}"
                _log.error(f"Memory command execution failed for present {present_name}, command {i + 1}: {cmd}, error: {e}")
                results.append(error_msg)
        
        return '\n'.join(results)
    
    def _execute_single_memory_command(self, cmd, file_path, cmd_index):
        """执行单个 memory 命令
        
        :param cmd: 单个命令
        :param file_path: memory 文件路径
        :param cmd_index: 命令索引（用于日志）
        :return: 命令执行结果
        """
        _log.debug(f"Executing memory command [{cmd_index}]: {cmd}")
        
        # read 命令
        if cmd == 'read':
            return self._memory_read(file_path)
        
        # write <content> 命令
        write_match = re.match(r'^write\s+(.+)$', cmd)
        if write_match:
            content = write_match.group(1).strip()
            if not content:
                return "(write 命令缺少内容)"
            return self._memory_write(file_path, content)
        
        # regex_search <pattern> 命令
        regex_match = re.match(r'^regex_search\s+(.+)$', cmd)
        if regex_match:
            pattern = regex_match.group(1).strip()
            if not pattern:
                return "(regex_search 命令缺少搜索模式)"
            return self._memory_regex_search(file_path, pattern)
        
        # delete <index> 命令
        delete_match = re.match(r'^delete\s+(\d+)$', cmd)
        if delete_match:
            try:
                idx = int(delete_match.group(1))
                return self._memory_delete(file_path, idx)
            except ValueError:
                return f"(delete 命令索引无效: {delete_match.group(1)})"
        
        # 未知命令
        _log.warning(f"Unknown memory command: {cmd}")
        return f"(未知 memory 操作: {cmd})"

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

            # 在添加用户消息前检查会话长度
            conversation = self.data['data'][conversation_dict][event.group_id]
            self._trim_conversation_if_needed(conversation)

            self.data['data'][conversation_dict][event.group_id].append({"role": "user", "content": user_message})
        else:
            conversation_dict = 'user_conversations'
            if event.user_id not in self.data['data'][conversation_dict]:  # 私聊消息
                self.data['data'][conversation_dict][event.user_id] = \
                    config.plugins_config['openai_chat_plugin']['presents']['default']['conversations']

            # 在添加用户消息前检查会话长度
            conversation = self.data['data'][conversation_dict][event.user_id]
            self._trim_conversation_if_needed(conversation)

            self.data['data'][conversation_dict][event.user_id].append({"role": "user", "content": user_message})

        try:
            # 添加用户消息到会话
            response = client.chat.completions.create(
                model=self.config['model'],
                messages=self.data['data'][conversation_dict][
                    event.group_id if event.message_type == 'group' else event.user_id]
            )
            reply_message = response.choices[0].message.content

            # 检查 memory 代码块
            memory_block = self._extract_memory_block(reply_message)
            if memory_block and self.config.get('replace_memory_block_in_reply', True):
                # 获取当前 present 名称
                if event.message_type == 'group':
                    present_name = None
                    for k, v in config.plugins_config['openai_chat_plugin']['presents'].items():
                        if v['conversations'] == self.data['data']['group_conversations'][event.group_id]:
                            present_name = k
                            break
                else:
                    present_name = None
                    for k, v in config.plugins_config['openai_chat_plugin']['presents'].items():
                        if v['conversations'] == self.data['data']['user_conversations'][event.user_id]:
                            present_name = k
                            break
                if not present_name:
                    present_name = 'default'
                memory_result = self._handle_memory_block(memory_block, present_name)
                reply_message = f"```memory\n{memory_result}\n```"
                await event.reply(reply_message)
                return

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
            _log.error(f"API 调用失败: {e}")
            # await event.reply("抱歉，AI 服务暂时不可用，请稍后再试。")

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
            "max_conversations", description="每个会话的最大消息数", allowed_values=["int"],
            default=21
        )
        self.register_config(
            "is_configured", description="插件是否已配置",
            allowed_values=["bool"],
            default=False
        )
        self.register_config(
            "allow_access_memory", description="是否允许访问记忆（memory 代码块）",
            allowed_values=["bool"],
            default=True
        )
        self.register_config(
            "replace_memory_block_in_reply",
            description="是否将 memory 代码块替换为实际 memory 操作结果",
            allowed_values=["bool"],
            default=True
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

        # 判断是否已配置
        if not self.config['is_configured']:
            _log.warning("插件未配置，请先配置插件。")

        # 检查是否正确添加了预设
        if not config.plugins_config['openai_chat_plugin']:
            _log.error("插件预设未正确添加，请检查预设。")

            # 设置`is_configured`为False
            self.config['is_configured'] = False

        # 检查并修剪所有现有会话，确保符合新的配置限制
        self._trim_all_conversations()

    @bot.group_event()
    async def on_group_message(self, event: GroupMessage):
        """处理群消息事件"""
        await self._handle_message(event)

    @bot.private_event()
    async def on_private_message(self, event: BaseMessage):
        """处理私聊消息事件"""
        await self._handle_message(event)
