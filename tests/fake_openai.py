# -*- coding: utf-8 -*-
"""OpenAI 客户端 stub：隔离真实网络，驱动对话链路测试。"""


class FakeMessage:
    """模拟 OpenAI 返回的 assistant 消息"""

    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeToolCall:
    """模拟模型发起的工具调用请求"""

    def __init__(self, call_id, name, arguments="{}"):
        self.id = call_id
        self.type = "function"
        self.function = type("F", (), {"name": name, "arguments": arguments})()


class FakeChoice:
    """模拟 choices[0]"""

    def __init__(self, message, finish_reason):
        self.message = message
        self.finish_reason = finish_reason


class FakeResponse:
    """模拟 chat.completions.create 的返回值"""

    def __init__(self, choice):
        self.choices = [choice]


class FakeCompletions:
    """记录调用参数并按脚本依次返回响应"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


class FakeChat:
    def __init__(self, responses):
        self.completions = FakeCompletions(responses)


class FakeOpenAIClient:
    def __init__(self, responses):
        self.chat = FakeChat(responses)


def make_client(reply="你好，我是AI"):
    """单轮直答客户端（finish_reason=stop）"""
    return FakeOpenAIClient([FakeResponse(FakeChoice(FakeMessage(content=reply), "stop"))])
