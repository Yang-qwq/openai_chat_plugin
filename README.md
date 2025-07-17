# OpenAI Chat Plugin

[![Version](https://img.shields.io/badge/version-0.1.0-blue.svg)](https://github.com/Yang-qwq/openai_chat_plugin)
[![License](https://img.shields.io/badge/license-AGPL-red.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.8+-blue.svg)](https://www.python.org/)

一个基于新版OpenAI SDK的智能聊天插件，为NcatBot提供强大的AI对话功能。

## ✨ 功能特性

- 🤖 **智能对话**：支持多种模型
- 💬 **群聊私聊**：同时支持群聊和私聊环境
- ⚙️ **多配置文件**：支持自定义多个对话预设
- 🔄 **会话管理**：支持重置和切换对话配置
- 🎯 **精确控制**：支持@机器人触发和用户名前缀
- 📝 **命令系统**：完整的命令控制界面
- 🧠 **智能会话管理**：自动控制会话长度，保护最近对话
- ⚡ **内存优化**：动态修剪超长会话，提升性能

## 📋 系统要求

- Python 3.8+
- NcatBot 框架
- OpenAI API Key

## 🚀 安装方法

### 方法一：手动安装

将插件源码放置到你的 `plugins` 目录下：

```bash
# 克隆插件到plugins目录
git clone https://github.com/Yang-qwq/openai_chat_plugin.git plugins/openai_chat_plugin
```

### 方法二：Git Submodule

```bash
cd /path/to/ncatbot/
git submodule add https://github.com/Yang-qwq/openai_chat_plugin.git plugins/openai_chat_plugin
```

## ⚙️ 配置说明

### 1. 基础配置

在NcatBot启动后，依次执行以下配置命令：

```bash
# 设置OpenAI API Key（强烈建议私聊发送）
/cfg OpenAIChatPlugin.api_key sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 设置OpenAI API Base URL（可选）
/cfg OpenAIChatPlugin.base_url https://api.openai.com/v1

# 设置使用的模型
/cfg OpenAIChatPlugin.model gpt-3.5-turbo

# 设置是否必须@机器人才能触发对话
/cfg OpenAIChatPlugin.must_at_bot true

# 设置是否在消息前添加用户名前缀（需要在prompt中声明）
# 开启之后，发给机器人的消息会自动添加用户名前缀
# 例如`User(ID): 你好`，则会在消息前添加`User(ID): `作为前缀
/cfg OpenAIChatPlugin.insert_userdata_as_prefix false

# 标记配置完成
/cfg OpenAIChatPlugin.is_configured true
```

### 2. 配置文件设置

在v0.1.0版本即之后的版本中，你无需在config.yaml中编辑预设，插件会在`data/openai_chat_plugin/presents/`目录中自动创建和管理预设配置文件。

想要编辑预设配置，可以直接编辑对应的`prompt.md`文件，或者使用命令行设置：

```bash
your_editor data/openai_chat_plugin/presents/default/prompt.md
```

## 📖 使用指南

### 基础对话

- **群聊**：在群聊中@机器人即可开始对话
- **私聊**：直接发送消息即可开始对话

### 命令系统

#### 设置配置文件

```bash
# 为当前环境设置配置文件
/chat set-present programmer

# 为指定群组设置配置文件
/chat set-present programmer group:1919810

# 为指定用户设置配置文件
/chat set-present translator user:114514
```

#### 重置会话

```bash
# 重置当前环境会话
/chat reset

# 重置指定群组会话
/chat reset group:1919810

# 重置指定用户会话
/chat reset user:114514
```

#### 获取帮助

```bash
# 显示帮助信息
/chat help
```

## 🔧 配置项说明

| 配置项                               | 类型      | 默认值                       | 说明                       |
|-----------------------------------|---------|---------------------------|--------------------------|
| `api_key`                         | string  | -                         | OpenAI API密钥             |
| `model`                           | string  | openai/gpt-4o-mini        | 使用的AI模型                  |
| `base_url`                        | string  | https://api.openai.com/v1 | API基础URL                 |
| `must_at_bot`                     | boolean | True                      | 群聊中是否必须@机器人              |
| `insert_userdata_as_prefix`       | boolean | False                     | 是否插入用户信息作为前缀             |
| `max_conversations`               | integer | 21                        | 每个会话的最大消息数               |
| `enable_builtin_function_calling` | boolean | False                     | 是否启用内置函数调用功能             |
| `allow_access_memory`             | boolean | False                     | 是否允许访问会话记忆（内置函数调用功能需要开启） |
| `is_configured`                   | boolean | False                     | 插件是否已配置                  |

## 🎯 高级功能

### 会话持久化

- 群聊会话独立存储
- 私聊会话独立存储
- 支持会话重置和配置切换

### 智能会话管理

插件具备智能的会话长度控制功能：

- **自动修剪**：当会话长度超过 `max_conversations` 限制时，自动删除最早的非system消息
- **保护最近对话**：优先保留最近的对话内容，删除最早的历史消息
- **配置自适应**：支持动态调整最大消息数，插件会自动清理超长会话
- **双重检查**：在添加新消息前后都进行长度检查，确保会话始终符合限制
- **主动清理**：插件加载时会自动检查并清理所有超长会话

**工作原理**：

1. 插件启动时检查所有现有会话
2. 删除最早的非system消息（保留预设配置）
3. 持续删除直到会话长度符合限制
4. 记录详细的修剪日志便于调试

## 🐛 故障排除

### 常见问题

1. **插件未响应**
    - 检查 `is_configured` 是否设置为 `true`
    - 确认API Key是否正确
    - 检查网络连接

2. **群聊中@机器人无响应**
    - 确认 `must_at_bot` 设置
    - 检查机器人QQ号是否正确

3. **配置文件不存在错误**
    - 检查 `config.yaml` 中的 `presents` 配置
    - 确认配置文件名称拼写正确

4. **会话长度异常**
    - 检查 `max_conversations` 配置值是否合理
    - 查看日志确认自动修剪功能是否正常工作
    - 确认预设配置中的system消息数量

### 日志查看

插件会输出详细的调试日志，可以通过日志查看运行状态：

```bash
# 查看插件日志
tail -f logs/ncatbot.log | grep openai_chat_plugin
```

## 📝 更新日志

### v0.1.0

- ✅ **支持内置Function Calling**：新增内置函数调用功能，允许插件调用预定义函数以增强交互能力
- ✅ **记忆访问控制**：新增配置项`allow_access_memory`，控制是否允许访问会话记忆，提升安全性和隐私保护

## 🤝 贡献指南

欢迎提交Issue和Pull Request来改进这个插件！

1. Fork 本仓库
2. 创建特性分支 (`git checkout -b feature/AmazingFeature`)
3. 提交更改 (`git commit -m 'Add some AmazingFeature'`)
4. 推送到分支 (`git push origin feature/AmazingFeature`)
5. 开启 Pull Request

## 📄 许可证

查看 [LICENSE](LICENSE) 文件了解详情。

## 🙏 致谢

- [NcatBot](https://github.com/ncatbot/ncatbot) - 优秀的机器人框架

---

⭐ 如果这个插件对你有帮助，请给个Star支持一下！
