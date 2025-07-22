# openai_chat_plugin

使用新版OpenAI SDK制作的聊天插件。


## 使用

你可以手动放置本项目的源码至你的 `plugins` 目录下。

当然，也可以使用`git submodule`来添加本项目。

```bash
cd /path/to/ncatbot/
git submodule add https://github.com/Yang-qwq/openai_chat_plugin.git plugins/openai_chat_plugin
```

在NcatBot启动后，请依次在聊天环境下对机器人输入以下命令来配置插件：

```
/cfg OpenAIChatPlugin.api_key sk-xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx  # 设置你的OpenAI API Key，此内容强烈建议私聊发送
/cfg OpenAIChatPlugin.base_url https://api.openai.com/v1  # 设置OpenAI API Base URL，具体请根据你使用的API服务商来设置
```

最后，请将以下内容添加到ncatbot的config.yaml中：

如果你的项目并没有`config.yaml`文件，请自行通过config类设置如下内容，具体方法可以参考[此文档](https://docs.ncatbot.xyz/guide/kfcvme50/#%E9%85%8D%E7%BD%AE%E9%A1%B9%E5%88%97%E8%A1%A8)
```yaml
# 来自插件的配置
plugins_config:
    openai_chat_plugin:
        # api_key, base_url等配置需要在聊天环境中设置，请移步至该插件的`README.md`中查看
        presents:  # 多配置文件将会在下个版本开发
            default:  # 不可删除
                display_name: "默认Agent"
                conversations:
                    - role: "system"
                      content: "You are a helpful assistant."
```

最后，请执行：

```
/cfg OpenAIChatPlugin.is_configured true  # 是否配置完成
```

后，重启NcatBot实例。

# TODO
- [ ] 支持多配置文件
- [ ] 支持多会话
- [ ] 支持私聊
- [ ] 支持命令控制
