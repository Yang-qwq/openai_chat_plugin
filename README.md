# OpenAI Chat Plugin

[![Version](https://img.shields.io/badge/version-0.1.7-blue.svg)](https://github.com/Yang-qwq/openai_chat_plugin)
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
/cfg OpenAIChatPlugin.ApiKey sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx

# 设置OpenAI API Base URL（可选）
/cfg OpenAIChatPlugin.BaseUrl https://api.openai.com/v1

# 设置使用的模型
/cfg OpenAIChatPlugin.Model gpt-3.5-turbo

# 设置是否必须@机器人才能触发对话
/cfg OpenAIChatPlugin.MustAtBot true

# 设置是否在消息前添加用户名前缀（需要在prompt中声明）
# 开启之后，发给机器人的消息会自动添加用户名前缀
# 例如`User(ID): 你好`，则会在消息前添加`User(ID): `作为前缀
/cfg OpenAIChatPlugin.InsertUserdataAsPrefix false

# 标记配置完成
/cfg OpenAIChatPlugin.IsConfigured true
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

#### 用户命令

```bash
# 为当前环境设置预设
/chat set-present programmer

# 重置当前环境会话
/chat reset

# 显示帮助信息
/chat help
```

#### 管理员命令

```bash
# 为当前环境设置预设
/chat-admin set-present programmer

# 为指定群组设置预设
/chat-admin set-present programmer group:1919810

# 为指定用户设置预设
/chat-admin set-present programmer user:114514

# 重置当前环境会话
/chat-admin reset

# 重置指定群组会话
/chat-admin reset group:1919810

# 重置指定用户会话
/chat-admin reset user:114514

# 批量更新所有会话的提示词（保留对话历史）
/chat-admin update-prompt all

# 更新指定群组/用户的提示词
/chat-admin update-prompt group:1919810
/chat-admin update-prompt user:114514

# 显示帮助信息
/chat-admin help
```

## 🔧 配置项说明

| 配置项                            | 类型      | 默认值                       | 说明                         |
|--------------------------------|---------|---------------------------|----------------------------|
| `ApiKey`                       | string  | -                         | OpenAI API密钥               |
| `Model`                        | string  | openai/gpt-4o-mini        | 使用的AI模型                    |
| `BaseUrl`                      | string  | https://api.openai.com/v1 | API基础URL                   |
| `MustAtBot`                    | boolean | True                      | 群聊中是否必须@机器人                |
| `InsertUserdataAsPrefix`       | boolean | False                     | 是否插入用户信息作为前缀               |
| `EnableBuiltinFunctionCalling` | boolean | False                     | 是否启用内置函数调用功能               |
| `AllowAccessMemory`            | boolean | False                     | 是否允许访问会话记忆（内置函数调用功能需要开启）   |
| `AllowWebRequests`             | boolean | False                     | 是否允许AI进行网络请求（内置函数调用功能需要开启） |
| `MaxRetriesTimes`              | integer | 15                        | 工具调用轮次的最大重试次数              |
| `IsConfigured`                 | boolean | False                     | 插件是否已配置                    |

## 🎯 高级功能

### 多配置文件支持

插件支持创建多个对话预设，每个预设可以有不同的系统提示词：

```
data/openai_chat_plugin/
| -- presents/
    | -- default/
        | -- config.yaml
        | -- prompt.md  <-- 系统提示词文件
    | -- programmer/
        | -- config.yaml
        | -- prompt.md  <-- 程序员预设的系统提示词文件
    | -- translator/
        | -- config.yaml
        | -- prompt.md  <-- 翻译预设的系统提示词文件
    ...
```

### 会话持久化

- 群聊会话独立存储
- 私聊会话独立存储
- 支持会话重置和配置切换

## 🐛 故障排除

### 常见问题

1. **插件未响应**
    - 检查 `IsConfigured` 是否设置为 `true`
    - 确认API Key是否正确
    - 检查网络连接

2. **群聊中@机器人无响应**
    - 确认 `MustAtBot` 设置
    - 检查机器人QQ号是否正确

3. **预设不存在错误**
    - 确认 `data/openai_chat_plugin/presents/` 目录下存在对应预设
    - 确认预设名称拼写正确

### 日志查看

插件会输出详细的调试日志，可以通过日志查看运行状态：

```bash
# 查看插件日志
tail -f logs/ncatbot.log | grep openai_chat_plugin
```

## 📝 更新日志

### v0.1.7

- 🛠️ **漏洞修复**：修复了上个版本出现的[路径穿越漏洞](https://github.com/Yang-qwq/openai_chat_plugin/issues/10)
- 🛠️ **性能修复**：持久化OpenAI客户端实例，避免重复创建导致的性能问题(https://github.com/Yang-qwq/openai_chat_plugin/issues/12)

### v0.1.6

- ⚙️ **配置项命名规范化**：所有配置项改为 PascalCase（如 `ApiKey`、`IsConfigured`），与 NcatBot 其他插件保持一致
- 🗑️ **移除会话长度限制**：彻底移除 `max_conversations` 配置及所有自动修剪/清理机制，会话历史将完整保留
- 注：由于修剪会话会导致部分平台缓存计费功能失效，故目前直接删除，之后会发布修复
- ✅ **支持更多函数调用**： 现在机器人可以初步感知聊天环境了
- ⚙️ **统一工具返回值生成**：将工具调用的错误处理重构为一个统一的函数，简化了代码结构并提升了可维护性
- ⚙️ **统一工具调用接口**：对所有工具调用进行标准化处理，提高了代码的一致性和可读性

### v0.1.5

- 🛠️ **bug修复**：修复错误的添加行为导致污染上下文的问题
- ✅ **支持提示词批量更新**：已创建会话中的提示词现在可以通过系统命令更新了
- ✅ **系统时间获取**：现在机器人可以通过调用读取系统时间

### v0.1.4

- ⚙️ **重构记忆存储结构**：增加`memory.json`中的新字段（`from_user`, `from_group`, `create_time`），同时重构了记忆调用，减少了工具数量
- ✅ **输出工具调用中间内容**：现在工具调用中间内容会被记录到上下文和输出了

### v0.1.3

- ⚙️ **变更会话修剪策略**：调整了会话修剪策略，现在会优先删除最早的非system消息，确保预设配置始终保留在会话中
- 🛠️ **bug修复**：修复`allow_access_memory`配置项未生效的问题，现在该配置项可以正确控制是否允许访问会话记忆

### v0.1.2

- ✅ **记录当前会话预设**：插件会记录当前会话使用的预设
- ✅ **对话日志总会打印**：无论是否启用调试模式，插件都会在日志中打印每次对话的预设名称和消息内容，便于追踪和调试

### v0.1.1

- ✅ **修复内置函数调用兼容性问题**：修复了在某些环境下内置函数调用可能无法正常工作的问题，提升了稳定性和可靠性

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
