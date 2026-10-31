# OpenAI Chat Plugin

[![Version](https://img.shields.io/badge/version-0.5.1-blue.svg)](https://github.com/Yang-qwq/openai_chat_plugin)
[![License](https://img.shields.io/badge/license-AGPL-red.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.10+-blue.svg)](https://www.python.org/)

一个基于新版OpenAI SDK的智能聊天插件，为NcatBot 5提供强大的AI对话功能。

## ✨ 功能特性

- 🤖 **智能对话**：支持多种模型
- 💬 **群聊私聊**：同时支持群聊和私聊环境
- ⚙️ **多配置文件**：支持自定义多个对话预设
- 🔄 **会话管理**：支持重置和切换对话配置
- 🎯 **精确控制**：支持@机器人触发和用户名前缀
- 📝 **命令系统**：完整的命令控制界面
- 🔐 **权限体系**：RBAC 全局管理员 + 群主/群管理自动放行
- 🌐 **网络请求**：AI 可抓取网页（HTML 自动转 Markdown）或调用 JSON API
- 🖼️ **图片视觉**：AI 可获取图片 URL 并直接“看到”图片内容
- 👁️ **用户图片识别**：用户发送的图片会被自动下载并交给视觉模型理解（可配置关闭）
- 🧠 **思维链隔离**：自动剥离 `<think>` / `<reasoning>` / DeepSeek-R1 全角分隔符等思维链，只写日志不外泄
- 🎲 **群聊随机插话**：按概率参与未 @机器人 的群聊，由模型自行决定是否发言（可群级关闭）
- ⏰ **AI 定时任务**：AI 可创建周期任务，到点用当前预设生成内容并投递回本会话
- 🗜️ **会话压缩**：超长会话历史自动压缩为摘要（写入记忆或插回会话），后台循环只处理长期闲置会话

## 📋 系统要求

- Python 3.10+
- NcatBot 5.x 框架
- OpenAI API Key

## 🚀 安装方法

### 方法一：手动安装

将插件源码放置到你的 `plugins` 目录下：

```bash
# 克隆插件到plugins目录
git clone https://github.com/Yang-qwq/openai_chat_plugin.git plugins/OpenAIChatPlugin
```

### 方法二：Git Submodule

```bash
cd /path/to/ncatbot/
git submodule add https://github.com/Yang-qwq/openai_chat_plugin.git plugins/OpenAIChatPlugin
```

## ⚙️ 配置说明

### 1. 基础配置

v0.2.0 起插件迁移到 NcatBot 5，配置统一保存在**全局 config.yaml** 的 `plugin.plugin_configs.OpenAIChatPlugin` 部分（也可通过 WebUI 修改）。首次启动后编辑全局 config.yaml：

```yaml
plugin:
  plugin_configs:
    OpenAIChatPlugin:
      ApiKey: sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 强烈建议仅在本地/私密环境填写
      BaseUrl: https://api.openai.com/v1               # 可选
      Model: openai/gpt-4o-mini                        # 可选
      MustAtBot: true                                  # 群聊是否必须@机器人
      InsertUserdataAsPrefix: false                    # 是否在消息前添加用户名前缀
      IsConfigured: true                               # 标记配置完成
```

修改后重启机器人生效。

### 2. 预设文件设置

插件会在插件工作区（`data/OpenAIChatPlugin/presets/`）目录中自动创建和管理预设配置文件。

> 从 v0.1.x 升级：首次启动时会自动将旧目录 `data/openai_chat_plugin/` 中的会话数据与预设迁移到新位置，
> 旧目录会被重命名为 `data/openai_chat_plugin.migrated.bak` 作为备份。
>
> 从早期 v0.2.x 升级：若工作区仍存在拼写错误的 `presents/` 目录，首次启动会自动重命名为 `presets/`。
>
> 从 v0.4.x 升级：首次启动会把集中存储在 `data.json` 里的会话历史一次性拆分到
> `group/<群号>.json` / `user/<QQ>.json` 分离文件（迁移前自动备份 `data.json.bak.<时间戳>`；
> 若有会话写出失败，旧数据原样保留，下次启动自动重试）。

想要编辑预设配置，可以直接编辑对应的`prompt.md`文件：

```bash
your_editor data/OpenAIChatPlugin/presets/default/prompt.md
```

## 📖 使用指南

### 基础对话

- **群聊**：在群聊中@机器人即可开始对话
- **私聊**：直接发送消息即可开始对话

### 命令系统

#### 用户命令

```bash
# 为当前环境设置预设
/chat set-preset programmer

# 重置当前环境会话
/chat reset

# 关闭 / 开启本群随机插话（不带参数查看当前状态）
/chat nolisten on
/chat nolisten off
/chat nolisten

# 显示帮助信息
/chat help
```

#### 管理员命令

需要**全局管理员**（机器人 owner 或 `/chatrbac` 授权）或**本群群主/群管理**身份（受 `EnableGroupOwnerAutoAuth` 开关控制，默认开启）：

```bash
# 为当前环境设置预设
/chat-admin set-preset programmer

# 为指定群组设置预设
/chat-admin set-preset programmer group:1919810

# 为指定用户设置预设
/chat-admin set-preset programmer user:114514

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

# 查看 / 删除 / 暂停 / 恢复 AI 定时任务
/chat-admin task list
/chat-admin task delete daily
/chat-admin task pause
/chat-admin task resume

# 显示帮助信息
/chat-admin help
```

> `task` 子命令涉及全局资源（查看他人任务、跨会话删除、全局暂停），仅限**全局管理员**
> （机器人 owner 或 `/chatrbac` 授权）使用，群主/群管理不可调用。

#### 全局管理员管理命令

仅限**全局管理员**（含机器人 owner）使用，群主/群管理身份不可越权调用：

```bash
# 授予全局管理员权限（支持 @ 成员或纯数字）
/chatrbac grant 888888

# 撤销全局管理员权限
/chatrbac revoke 888888

# 查看全局管理员列表
/chatrbac list

# 显示帮助信息
/chatrbac help
```

## 🔧 配置项说明

| 配置项                            | 类型    | 默认值                    | 说明                                                                                    |
|-----------------------------------|---------|---------------------------|-----------------------------------------------------------------------------------------|
| `ApiKey`                          | string  | -                         | OpenAI API密钥                                                                          |
| `Model`                           | string  | openai/gpt-4o-mini        | 使用的AI模型                                                                            |
| `BaseUrl`                         | string  | https://api.openai.com/v1 | API基础URL                                                                              |
| `MustAtBot`                       | boolean | True                      | 群聊中是否必须@机器人                                                                   |
| `InsertUserdataAsPrefix`          | boolean | False                     | 是否插入用户信息作为前缀                                                                |
| `EnableBuiltinFunctionCalling`    | boolean | False                     | 是否启用内置函数调用功能                                                                |
| `AllowAccessMemory`               | boolean | False                     | 是否允许访问会话记忆（内置函数调用功能需要开启）                                        |
| `AllowWebRequests`                | boolean | False                     | 是否允许AI进行网络请求（内置函数调用功能需要开启）                                      |
| `AllowLoopbackIps`                | boolean | False                     | 是否允许网络/图片请求访问回环地址（如 127.0.0.1、::1）                                  |
| `AllowLocalNetworkIps`            | boolean | False                     | 是否允许网络/图片请求访问内网地址（如 10.x、192.168.x、fc00::）                         |
| `EnableImageRecognition`          | boolean | True                      | 是否识别用户消息中携带的图片并作为视觉输入交给模型（无需开启函数调用）                  |
| `StripReasoning`                  | boolean | True                      | 是否剥离推理模型的思维链（`<think>` / `<reasoning>` / DeepSeek 全角分隔符等），仅写日志 |
| `EnableRandomReplyGroup`          | boolean | False                     | 是否允许群聊随机插话（未 @机器人 的消息按概率参与）                                     |
| `GroupRandomReplyChance`          | number  | 0.02                      | 群聊随机插话概率（0~1），命中后仍由模型决定是否发言                                     |
| `AllowScheduledTasks`             | boolean | False                     | 是否允许 AI 创建/管理定时任务（内置函数调用功能需要开启）                               |
| `EnableScheduledTaskExecution`    | boolean | True                      | 定时任务执行总开关（kill switch）；关闭后不注册/不执行，记录保留                        |
| `MaxRetriesTimes`                 | integer | 15                        | 工具调用轮次的最大重试次数                                                              |
| `MaxConversationCacheStoreAmount` | integer | 1000                      | 最大对话缓存存储数量；也是 `query_missing_message_context` 取回条数的上限              |
| `EnableGroupOwnerAutoAuth`        | boolean | True                      | 群主/群管理自动放行本群管理员命令                                                       |
| `EnableConversationCompression`   | boolean | False                     | 是否启用会话压缩（超过阈值的历史压缩为摘要）；单独开启即启动后台循环                    |
| `EnableAutoCleanup`               | boolean | False                     | 后台压缩循环总开关（与上一项任一开启都会启动循环）                                      |
| `AutoCleanupInterval`             | integer | 1800                      | 自动清理**检查**间隔（秒，默认 30 分钟）；只扫描会话文件，本身不消耗 API 额度；`<=0` 回退 1800 |
| `AutoCleanupIdleSeconds`          | integer | 28800                     | 会话**闲置多久**才允许自动压缩（秒，默认 8 小时）；活跃会话不会被压缩                   |
| `CompressionThresholdBytes`       | integer | 1048576                   | 触发压缩的会话字节数阈值（默认 1MB，需按模型上下文调整）                                |
| `CompressionKeepRecentBytes`      | integer | 524288                    | 压缩时保留的**最近**会话字节数（默认 512KB，尾部窗口）                                  |
| `IsConfigured`                    | boolean | False                     | 插件是否已配置                                                                          |

## 🎯 高级功能

### 用户图片识别

默认开启（`EnableImageRecognition: true`）。当用户发送的消息中携带图片时，插件会
自动识别并下载这些图片，将其作为 OpenAI 视觉输入（`image_url`）附加到对应对话，
模型即可直接“看到”用户发的图。无需开启 `EnableBuiltinFunctionCalling`。

- 群聊 / 私聊均支持；群聊仍受 `MustAtBot` 约束（需 @机器人）
- 一条消息携带多张图片时，全部作为独立的视觉输入附加
- 仅发图片不带文字时，文本部分自动补一句默认说明（如“用户发送了图片，请查看随附图片。”）
- 文本部分仅取消息中的纯文字（不会把 `[CQ:image,...]` 等消息段标记交给模型）
- 图片按文件头魔数校验类型，支持 PNG / JPEG / GIF / WEBP / BMP；
  下载失败、格式不受支持或超过大小上限（5 MiB）的图片会被忽略，不影响文本部分的处理
- 关闭：在全局 config.yaml 的 `plugin_configs.OpenAIChatPlugin` 中设置
  `EnableImageRecognition: false`

**纯文本模型自动降级**：若模型/网关只接受 `content.type='text'`（如智谱 GLM 返回
400 code 1210 `messages.content.type 参数非法，取值范围 ['text']`），插件会在首次
失败后用纯文本重试（历史图片替换为 `[图片]`，会话历史本身不改），并记住该模型不支持
视觉：后续请求提前压平、不再下载用户图片、工具图片只保留摘要。无关的 400 不会触发降级。

> 注意：图片识别默认开启即意味着插件会下载用户发送的图片。若不希望插件访问图片
> 资源（例如仅纯文本对话、或处于受限网络环境），请将 `EnableImageRecognition` 置为 false。

### 网络请求与图片视觉

需要同时开启 `EnableBuiltinFunctionCalling` 与 `AllowWebRequests`。开启后模型可以调用
`create_http_request` 工具：

| `content_format` | 用途                                 |
|------------------|--------------------------------------|
| `text`           | 获取纯文本响应                       |
| `markdown`       | 抓取网页，HTML 会自动转换为 Markdown |
| `json`           | 调用 JSON API 并返回结构化数据       |
| `image`          | 获取图片并作为视觉输入交给模型阅读   |

示例（模型自动完成，无需手动输入）：

```text
用户：@机器人 帮我总结 https://example.com/article 的内容
用户：@机器人 看看 https://example.com/cat.png 这张图里有什么

模型 -> create_http_request(method="GET", url="...", content_format="markdown")
模型 -> create_http_request(method="GET", url="...", content_format="image")
```

安全与限制：

- 仅允许 `http/https`，`GET/POST/PUT/PATCH/DELETE/HEAD/OPTIONS`
- 默认超时 15 秒（最大 60 秒）
- 文本响应上限 2 MiB、图片上限 5 MiB，超出会中止读取
- 请求失败/参数非法/超时都会以工具错误结果返回，不会中断对话
- 图片会以 data URL 形式附加到上下文；工具历史只保留摘要，避免 base64 占满上下文
- 网络请求具有访问内网的潜在风险，请仅在可信环境开启 `AllowWebRequests`
- **SSRF 防护**：插件在每一跳请求前校验目标主机，默认仅允许公网地址——
  - 回环（`127.0.0.1`、`::1`）与内网段（`10/8`、`172.16/12`、`192.168/16`、
    `fc00::/7` 等）分别受 `AllowLoopbackIps` / `AllowLocalNetworkIps` 控制，
    默认拒绝
  - 链路本地地址（含云元数据 `169.254.169.254`）、未指定、组播、保留段等
    **一律拒绝，不提供放行开关**
  - 重定向由插件手动逐跳跟随并重新校验，公网 URL 302 到内网地址也会被拦截；
    跨源重定向不会携带 `Authorization` / `Cookie`，`Host` 按目标主机重建
  - 网络错误的提示文案统一为「请求失败」，不泄露具体是超时还是连接被拒
  - 用户发送图片的下载链路共用同一套校验

### 思维链隔离

默认开启（`StripReasoning: true`）。部分推理模型会把思考过程混进正文，插件会在发送与
写入会话历史之前统一剥离：

- `<think>` / `<thinking>` / `<reasoning>` / `<thought>` / `<analysis>` 成对标签
- 未闭合的开标签：连同其后的全部内容一并移除（宁缺毋滥）
- DeepSeek-R1 全角分隔符 `<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>`
- provider 已剥掉开标签、只剩 `</think>` 的孤儿闭标签：其前的思维链残留一并移除

思维链（含 API 的 `reasoning_content` 字段）只以 `AI思维链:` 写入日志，不会发送给用户；
若模型仅返回思维链而无正文，插件保持沉默而不是把思考过程发出去。

### 群聊随机插话

关闭时群聊仍需 @机器人（`MustAtBot`）。开启 `EnableRandomReplyGroup` 后，未 @机器人 的
群消息会按 `GroupRandomReplyChance` 概率参与对话：

- 命中后仍由模型决定是否发言：模型输出 `NO_REPLY` 或空正文时插件保持沉默
- **`NO_REPLY` 哨兵对所有对话轮生效**（群 @、私聊、随机插话、定时任务）：模型显式回复
  `NO_REPLY`（容忍 `` `NO_REPLY` ``、`no-reply` 等写法）时不发送，归一化的沉默决定
  写入会话历史以保持轮次完整——历史里落库的 `NO_REPLY` 会教会模型在普通轮也回哨兵，
  不加识别就会把字面文本原样发给用户。纯空正文（如仅返回思维链）同样不发送，但普通
  轮不追加该轮到历史（见「思维链隔离」）
- 临时提示不描述性格（人设、语气仍由预设 `prompt.md` 决定），只决定「是否发言」：
  开启函数调用时鼓励模型调用 `query_missing_message_context` 查看错过的群消息
  （该工具支持 `count` 参数，默认 50 条，上限为 `MaxConversationCacheStoreAmount`；
  取回成功后清空该群临时缓存，视为已消费）；
  未开启函数调用时才由插件附带最近最多 20 条群聊记录（只存在于本次请求副本，
  不写入会话历史）作为兜底。提示词包含正向的发言条件，并在拿不准时倾向简短接一句，
  而不是默认沉默
- 随机插话轮同样保留工具调用能力（与普通聊天一致，受 `EnableBuiltinFunctionCalling`
  与各 `AllowXxx` 开关约束）；工具轮在发言成功或沉默（`NO_REPLY`）时都写入会话历史——
  沉默时工具调用结果与归一化的 `NO_REPLY` 决定一并保留（哨兵变体与空正文统一记为
  `NO_REPLY`），仅异常（限流/工具上限/内部错误）时整轮回滚，不会污染上下文
- 工具轮产生的中间正文不会投递到群里，避免「先说话再沉默」；工具轮上限与内部错误
  同样静默处理
- 同一群有 30 秒冷却，避免连续刷屏；机器人自己的消息不参与
- 群内任何人可用 `/chat nolisten on` 关闭本群随机插话（`off` 重新开启，不带参数查看状态）
- 该开关保存在插件工作区 `data.json` 的 `group_nolisten` 中
- 随机插话的临时提示固定使用 user 角色（预设提示词仍是唯一的 system），
  兼容只接受单个 system 的供应商（如智谱 GLM、AMD Radeon 网关）
- 请求遇瞬时限流（429）时由 OpenAI SDK 自动指数退避重试；重试耗尽仍被限流时
  随机插话静默放弃（不往群里发错误提示），@/私聊则回复「请求过于频繁，请稍后再试」

### AI 定时任务

需要同时开启 `EnableBuiltinFunctionCalling` 与 `AllowScheduledTasks`。开启后模型可调用
`manage_cron_tasks` 工具：

| action   | 说明                                                               |
|----------|--------------------------------------------------------------------|
| `add`    | 创建任务（投递到当前会话），需要 `task_name`、`interval`、`prompt` |
| `delete` | 删除任务，需要 `task_name`；只能删除**当前会话**创建的任务         |
| `list`   | 查看所有会话的任务与下次执行时间                                   |

- 到点后插件用「主会话历史 + 一条内部消息 `![定时任务 <name>] <prompt>`」执行一次完整对话轮次
  （**支持工具调用**，与普通聊天共用 `EnableBuiltinFunctionCalling` 与各 `AllowXxx` 开关），
  再把生成的内容投递回创建者会话（模型无法指定其他群/私聊，避免越权骚扰）
- 投递成功后，整轮消息（内部消息、工具调用轮次、最终 assistant 回复）会一起写入主会话历史；
  生成或投递失败则整轮不落库，不会在历史里留下半截对话
- 模型主动选择沉默（回复 `NO_REPLY` 哨兵）时同样整轮写入历史（归一化的沉默决定），
  并视为执行成功——沉默是模型的有效决定而非失败，不累计失败计数；空正文仍按失败计
- 工具调用前的中间正文也会投递到会话（与普通聊天一致）；任务记录会保存 `creator_id`，
  供记忆/环境工具识别来源
- 定时轮与普通聊天共用会话级锁，同一会话的轮次串行执行，避免消息顺序错乱
- `interval` 支持 `30s`、`2h30m`、`0.5d`、`HH:MM`（每日）、`YYYY-MM-DD HH:MM:SS`（一次性）
- **最小间隔写死 60 秒**：间隔任务不足 60 秒会被拒绝，一次性任务也必须距离当前时间不少于
  60 秒（避免 `0.5s` 之类的高频任务刷屏/耗尽额度）
- 任务**不做会话隔离**：`list` 能看到其他会话的任务（含目标会话 ID 与提示词），但 `add`
  只能创建到当前会话、`delete` 只能删除本会话创建的任务，跨会话删除会返回错误
- 任务记录保存在 `data.json` 的 `scheduled_tasks`，重启后自动重新注册；interval 小于 60 秒
  的历史记录不会被注册（记录保留，可通过删除清理）
- 任务名仅允许字母/数字/下划线/连字符（1~32 字符）；同一插件内不可重名
- 连续执行失败 5 次后自动停用（移出调度器并在记录里标记 `disabled`，重启不再注册），
  避免机器人被踢出群、密钥失效等永久性错误按原间隔无限重试
- 一次性任务执行后自动清理记录，不会在列表中残留
- 运维命令（走 `/chat-admin` 三层权限校验）：
  - `/chat-admin task list`：查看全部任务（所属会话、状态、连续失败次数、下次执行时间）
  - `/chat-admin task delete <name>`：删除任意任务（可跨会话清理）
  - `/chat-admin task pause` / `resume`：暂停/恢复执行总开关 `EnableScheduledTaskExecution`，
    暂停时任务记录保留，恢复后重新注册未停用的任务
- 同步 OpenAI 请求通过线程池（`asyncio.to_thread`）执行，单请求默认超时 60 秒；
  429/5xx 由 SDK 指数退避重试 4 次（并遵守 `Retry-After`），定时任务不会阻塞事件循环

### 多配置文件支持

插件支持创建多个对话预设，每个预设可以有不同的系统提示词：

```
data/OpenAIChatPlugin/
| -- data.json            <-- 会话状态（预设名、群开关、定时任务记录）
| -- group/               <-- 群会话历史（v0.5.0 起按会话独立文件）
|   | -- <群号>.json
| -- user/                <-- 私聊会话历史
|   | -- <QQ号>.json
| -- presets/
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

- 群聊 / 私聊会话各自独立存储（`group/<群号>.json` / `user/<QQ号>.json`，纯消息数组）
- 支持会话重置和配置切换；预设名等小状态仍在 `data.json`
- 会话文件原子写入（临时文件 + 替换），不会因为进程中断留下半截 JSON

### 会话压缩

开启 `EnableConversationCompression` 后，超过 `CompressionThresholdBytes` 的会话历史会被
压缩为摘要，避免长对话逐渐撑爆模型上下文：

- 后台压缩循环由压缩开关或 `EnableAutoCleanup` 任一开启即启动（无需两个都开），
  默认每 `AutoCleanupInterval`（30 分钟）检查一次，**只处理闲置超过
  `AutoCleanupIdleSeconds`（默认 8 小时）的会话**，检查本身不消耗 API 额度
- 会话首条 system 提示词（预设人格）不会被压缩；保留**最近** `CompressionKeepRecentBytes`
  字节的消息，只把更早的历史汇总成摘要
- 同时开启 `EnableBuiltinFunctionCalling` + `AllowAccessMemory` 时，摘要写入当前预设的
  记忆文件（`memory.json`）；否则摘要作为一条 `[历史摘要] …` 消息插在 system 提示词**之后**
- 摘要只包含文本内容（图片以 `[图片]` 占位），base64 不会进入摘要；被压缩历史很长时
  摘要输入会等距抽样并写警告日志
- 摘要生成失败、或记忆写入失败，都不会修改会话历史（原样保留，下次重试）
- 默认 1 MB 阈值按 200k+ 上下文模型估算，**128k 上下文模型建议调低**
  （例如 `CompressionThresholdBytes: 300000`、`CompressionKeepRecentBytes: 100000`），
  否则会话会先撞到模型上下文上限

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
    - 确认 `data/OpenAIChatPlugin/presets/` 目录下存在对应预设
    - 确认预设名称拼写正确

4. **管理员命令提示权限不足**
    - 确认使用者为机器人 owner 或已通过 `/chatrbac grant` 授权
    - 群内使用可依赖群主/群管理身份（检查 `EnableGroupOwnerAutoAuth` 是否开启）

5. **开启压缩后会话没变短**
    - 压缩只由后台循环触发：确认 `EnableConversationCompression`（或 `EnableAutoCleanup`）已开启，
      且插件重启过（循环在加载时启动）
    - 只压缩**闲置超过 `AutoCleanupIdleSeconds`（默认 8 小时）**且超过
      `CompressionThresholdBytes`（默认 1 MB）的会话；活跃会话不会被动
    - 日志里出现「已等距抽样」说明单次摘要预算不够覆盖整段历史，可调低阈值
    - 压缩后仍报上下文超限：阈值相对模型上下文过大，参考上面的调低建议

### 日志查看

插件会输出详细的调试日志，可以通过日志查看运行状态：

```bash
# 查看插件日志
tail -f logs/ncatbot.log | grep openai_chat_plugin
```

## 📝 更新日志

### v0.5.2

- 🤫 **`NO_REPLY` 沉默推广到所有对话轮**：此前只有随机插话轮识别「不发言」哨兵，
  普通 @/私聊与定时任务轮会把字面的 `NO_REPLY` 原样发给用户（历史里落库的哨兵
  记录会教会模型在普通轮也回哨兵）。现在所有轮次统一识别哨兵（容忍
  `` `NO_REPLY` ``、`no-reply` 等写法）：不发送/不投递，归一化的沉默决定写入
  会话历史以保持轮次完整；普通轮的纯空正文（如仅返回思维链）仍维持不追加历史
  的旧行为
- ⏰ **定时任务的主动沉默计为成功**：定时轮回 `NO_REPLY` 时不再投递、整轮
  （内部消息、工具轮、沉默决定）写入历史，且**不累计失败计数**——沉默是模型的
  有效决定而非故障，连续沉默不会触发自动停用；空正文仍按失败计
- 🧹 **可读性优化**：沉默判定拆分为 `_is_no_reply_sentinel()`（原
  `_is_silent_reply()`），调用方分支语义自明；抽出 `_commit_working_turn()`
  （工作副本提交）与 `_session_tag()`（日志会话前缀）两个小工具，消除回合管线
  中的重复代码

### v0.5.1

- 🔧 **修复随机插话提示插错位置**：工具轮第二次请求曾把临时提示插到
  `assistant(tool_calls)` 与 `tool` 结果之间，破坏工具消息的相邻顺序（严格网关会 400），
  也让提示看起来像「对工具结果的备注」；现在按本次用户消息的固定下标插入。
- 🎲 **随机插话不再几乎必然沉默**：提示词补回「什么时候适合发言」的正向条件与
  「拿不准时倾向接一句」的默认，移除「调用过工具就必须 NO_REPLY」的表述；同时明确
  「不要为了决定是否插话而反复查记忆」，避免模型在随机轮反复调用 `access_memory`
  后判定无话可说。提示词不描述性格，人设仍完全由预设 `prompt.md` 决定
- 💬 **随机插话的上下文获取**：开启函数调用时，提示词鼓励模型主动调用
  `query_missing_message_context` 查看被忽略的群消息；该工具新增 `count` 参数
  （默认 50 条），并在分派链按 `MaxConversationCacheStoreAmount` 截断，避免一次拉回
  整段缓存撑爆上下文；**取回成功后清空该群的全部临时消息**（视为已消费）。函数调用
  不可用时才由插件附带最近最多 20 条群聊记录
  （`temp_history_messages_kv`，不写入会话历史、不重复会话里已有的内容、单条截断、
  剔除含 `NO_REPLY` 的发言）。`_record_group_history` 改为始终记录（纯内存，仍受
  `MaxConversationCacheStoreAmount` 限制）

### v0.5.0

- 🗂️ **会话分离文件存储**：会话历史不再集中塞在 `data.json`，改为按会话独立文件
  `group/<群号>.json` / `user/<QQ号>.json`（纯消息数组，原子写入）。从 v0.4.x 升级时
  首次启动自动迁移：迁移前备份 `data.json.bak.<时间戳>`，全部成功才删除旧键，部分失败
  保留旧数据下次重试；已存在的分离文件不会被旧数据覆盖
- 🗜️ **会话压缩**：新增 `EnableConversationCompression` / `CompressionThresholdBytes` /
  `CompressionKeepRecentBytes`，超长会话压缩为摘要，避免长对话撑爆模型上下文
  - 保留**最近** `CompressionKeepRecentBytes` 字节的消息，只压缩更早的历史；
    预设 system 提示词永不参与压缩
  - 摘要写入预设 `memory.json`（开启函数调用 + 记忆时），否则作为 `[历史摘要] …`
    消息插在 system 提示词之后
  - 摘要生成或记忆写入失败时不修改会话历史；图片以 `[图片]` 占位，base64 绝不进入摘要
  - 新增 `EnableAutoCleanup` / `AutoCleanupInterval` 后台循环：默认每 30 分钟检查一次，
    只压缩**闲置超过 8 小时**的会话（任一开关开启即启动，卸载时自动取消）
- 🛠️ **健壮性**：会话文件损坏时自动按默认预设重建，不再让该会话永久报错；
  `group_preset_names` / `user_preset_names` 键统一为字符串，修复
  `/chat-admin set-preset group:<id>` 后 `update-prompt all` 误用默认预设覆盖提示词
- 📦 **目录结构调整**：新增 `session_store.py`（分离文件存储层）、`update.py` 拆分迁移；
  测试新增 `test_session_split.py`、`test_compression.py`

### v0.4.4

- 🖼️ **纯文本模型兼容**：历史中的 `image_url` 不再让只支持纯文本的供应商（如智谱 GLM
  400 code 1210）永久失败

### v0.4.3

- 🛡️ **SSRF 防护**：HTTP 工具与用户图片下载链路在每一跳请求前校验目标主机，修复开启 `AllowWebRequests` 后可经 `http://169.254.169.254/…`、`http://127.0.0.1:…` 等读取云元数据/内网服务的问题（[issue #23](https://github.com/Yang-qwq/openai_chat_plugin/issues/23) R1）
  - 回环地址（含 IPv4-mapped 写法）受新配置 `AllowLoopbackIps` 控制（默认关）、内网段受 `AllowLocalNetworkIps` 控制（默认关）；链路本地（含云元数据）、未指定、组播、保留段等一律拦截，无放行开关
  - 域名主机解析 DNS 后对全部 A/AAAA 记录逐条校验，任一命中即拒绝；DNS 解析失败同样拒绝
  - 重定向改由插件手动逐跳跟随（上限 5 跳），每一跳重新校验主机与协议，防止「公网 URL 302 到内网地址」绕过；301/302/303 降级 POST 为 GET（与原 httpx 行为一致），307/308 保持方法与请求体
  - 网络错误文案统一为「请求失败：…」，不再区分超时/连接失败，避免被当作端口与路径探测的 oracle
  - 模型无法通过工具参数自行放行受限地址（同名参数一律忽略，策略只由运营配置决定）

### v0.4.2

- 🔧 **移除 `ReplaceSystemAsUser` 配置**：随机插话的临时提示现在固定使用 user 角色（预设提示词仍是唯一的 system）。第二条 system 会被严格网关以 400 `System message must be at the beginning.` 拒绝（实测 AMD Radeon 网关 + Qwen3.8-27B），该开关已无存在必要，部署中如有设置可直接删除
- 🛡️ **429 限流容错**：OpenAI 请求最大重试次数从 1 提高到 4（SDK 指数退避 + 遵守 `Retry-After`）；重试耗尽仍限流时随机插话静默放弃，@/私聊回复「请求过于频繁，请稍后再试」，不再报「插件出现内部错误」

### v0.4.1

- 🛠️ **定时任务重构**：执行轮支持工具调用并写入主会话历史，投递成功才落库；定时轮与普通聊天共用会话级锁，避免消息交错
- 🔐 **运维命令**：新增 `/chat-admin task list|delete|pause|resume`；`task` 涉及跨会话/全局资源，仅全局管理员可用（防群主提权）
- ⚙️ **执行总开关**：新增 `EnableScheduledTaskExecution`（默认开启），关闭后不注册/不执行、记录保留
- 🛡️ **失败保护**：最小调度间隔写死 60 秒；连续失败 5 次自动停用并移出调度器；一次性任务执行后清理记录
- 🛠️ **稳定性**：OpenAI 请求改在线程池执行避免阻塞事件循环，并收紧单请求超时（60 秒）与重试次数

### v0.4.0

- 🧠 **思维链隔离**：移植 NcatBotTRPG 的思维链剥离逻辑，自动移除 `<think>` / `<reasoning>` / DeepSeek 全角分隔符与孤儿闭标签；思维链只写日志，不进入回复与会话历史（新增 `StripReasoning`，默认开启）
- 🎲 **群聊随机插话**：未 @机器人 的群消息按概率参与，命中后由模型决定是否发言（`NO_REPLY`/空正文即沉默）；新增 `EnableRandomReplyGroup`、`GroupRandomReplyChance`、`/chat nolisten` 群级开关与 30s 冷却
- ⏰ **AI 定时任务**：新增 `manage_cron_tasks` 工具（add/delete/list），到点用当前预设生成内容并投递回创建者会话；任务落 `data.json`、重启恢复，受 `AllowScheduledTasks` 控制（默认关闭）
- 🌐 **GLM 兼容**：新增 `ReplaceSystemAsUser`，把随机插话等临时 system 提示降级为 user，保证只有预设是 system
- 🧪 **测试**：补齐思维链、随机插话与定时任务测试；NcatBotTRPG 不存在时自动跳过跨插件协调测试

### v0.3.0

- 👁️ **用户图片识别**：识别并下载用户消息中携带的图片，作为 `image_url` 视觉输入交给模型（群聊/私聊均支持，多图全部附加）；新增 `EnableImageRecognition` 配置开关（默认开启），支持 PNG/JPEG/GIF/WEBP/BMP，下载失败或格式不受支持时优雅降级为纯文本
- 🧪 **测试**：新增用户图片识别链路测试（多图、关闭开关、下载失败降级）

### v0.2.2

- 🌐 **网络请求工具**：新增 `create_http_request`，支持 text / markdown / json / image，受 `AllowWebRequests` 控制
- 🖼️ **图片视觉**：获取的图片会作为 `image_url` 视觉消息附加到对话，可直接阅读图片内容
- 🛡️ **请求防护**：方法/协议白名单、超时、响应大小限制，网络异常统一转为工具错误结果
- 🧪 **测试**：补齐 HTTP 工具与视觉链路测试，兼容 pytest-asyncio ≥ 1.0
- 🛠️ **修复空中间正文**：跳过工具调用轮中仅空白的中间正文，避免发送空消息

### v0.2.1

- 🛠️ **预设管理重构**：`present_manager.py` 重命名为 `preset_manager.py`，新增 `Preset` 类统一加载 `config.yaml` 与 `prompt.md`
- 🛠️ **修正拼写**：预设目录 `presents/` 统一为 `presets/`，首次启动自动重命名旧目录；命令 `/chat set-present` 更名为 `/chat set-preset`
- 📦 **迁移逻辑重构**：拆分 4.x 工作区数据迁移与记忆格式升级，迁移失败不再阻塞插件加载

### v0.2.0

- 🚀 **迁移到 NcatBot 5**：全面适配 NcatBotPlugin / registrar 装饰器注册 / api.qq 多平台 API
- 🛠️ **漏洞修复**：修复 v0.1.x 中重复定义的群消息处理器相互覆盖、导致群聊 AI 对话失效的问题
- 🔐 **权限体系重构**：`/chat-admin` 改为三层权限体系（RBAC 全局管理员 + 群主/群管理自动放行 + 默认拒绝）；新增 `/chatrbac` 命令管理全局管理员
- ⚙️ **配置方式变更**：配置统一保存到全局 config.yaml 的 `plugin_configs.OpenAIChatPlugin`（原 `/cfg` 命令不再可用）
- 📦 **数据自动迁移**：首次启动自动将旧版 `data/openai_chat_plugin/` 的会话数据与预设迁移到新位置，旧目录保留为 `.migrated.bak` 备份
- 🧰 **工具适配**：查询类工具适配 v5 pydantic 返回值；回复消息不再 @ 发送者（与旧版行为一致）

### v0.1.9

- 🛠️ **精简代码**：删除部分迁移逻辑
- ✅ **支持更多函数调用**： 机器人可以获取上下文信息与系统时间等内容了
- 🛠️ **修正拼写**：新增 `Preset` 类：替代原 `present_manager.py` 中的函数（`present`/`presents` 已统一修正为 `preset`/`presets`）

### v0.1.8

- 🛠️ **错误处理增强**：修改部分错误处理规则
- ⚙️ **省略处理规范**：所有省略部分现在可通过源码修改

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
