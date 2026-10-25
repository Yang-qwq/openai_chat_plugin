# -*- coding: utf-8 -*-
"""推理模型思维链剥离

移植自同作者插件 NcatBotTRPG（AGPL-3.0）的 ``llm.py``：

- 来源仓库：https://github.com/Yang-qwq/NcatBotTRPG
- 原始文件：``llm.py`` 中的 ``strip_reasoning`` / ``extract_reasoning`` 及其正则常量
- 许可证：GNU Affero General Public License v3.0（与本插件一致）
- 修改：抽取为独立模块，日志记录器改为本插件；逻辑保持一致

支持的形式：

1. 常见成对标签：``<think>…</think>``、``<thinking>…</thinking>``、
   ``<reasoning>…</reasoning>``、``<thought>…</thought>``、``<analysis>…</analysis>``
2. 未闭合的开标签：连同其后的全部内容一并移除（宁缺毋滥）
3. DeepSeek-R1 风格全角分隔符：``<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>``
4. 孤儿闭标签：provider 已剥离开标签、只剩 ``</think>`` 等时，
   其前的思维链残留一并移除

思维链只用于日志（``extract_reasoning``），绝不发送给用户或写入会话历史。
"""

import re

from ncatbot.utils.logger import get_log

__all__ = ['extract_reasoning', 'strip_reasoning']

_log = get_log('openai_chat_plugin.reasoning')

# 1) 常见成对标签：<think>…</think>、<thinking>…</thinking>、<reasoning>…</reasoning> 等
_REASONING_TAGS = ('think', 'thinking', 'reasoning', 'thought', 'analysis')
_REASONING_BLOCK_RES = [
    re.compile(rf'<{tag}\b[^>]*>.*?</{tag}\s*>', re.IGNORECASE | re.DOTALL)
    for tag in _REASONING_TAGS
]
# 2) 未闭合的思维链标签：连同其后的全部内容一并移除，宁缺毋滥
_REASONING_OPEN_RES = [
    re.compile(rf'<{tag}\b[^>]*>.*$', re.IGNORECASE | re.DOTALL)
    for tag in _REASONING_TAGS
]
# 3) DeepSeek-R1 风格全角分隔符：<｜begin▁of▁thinking｜>…<｜end▁of▁thinking｜>
_REASONING_SPECIAL_RE = re.compile(
    r'<[|｜][^>]*begin[^>]*thinking[^>]*>.*?<[|｜][^>]*end[^>]*thinking[^>]*>',
    re.IGNORECASE | re.DOTALL,
)
_REASONING_SPECIAL_OPEN_RE = re.compile(
    r'<[|｜][^>]*begin[^>]*thinking[^>]*>.*$',
    re.IGNORECASE | re.DOTALL,
)
# 4) 孤儿闭标签（缺少开标签）：provider 可能已剥离开标签，只剩 </think> / <｜end▁of▁thinking｜>
_ORPHAN_CLOSE_RE = re.compile(
    r'</(?:think(?:ing)?|reasoning|thought|analysis)\s*>', re.IGNORECASE
)
_ORPHAN_CLOSE_DS_RE = re.compile(
    r'<[|｜][^>]*end[^>]*thinking[^>]*>', re.IGNORECASE
)
# 去除思维链标签，仅保留思考内容（用于日志提取）
_TAG_STRIP_RE = re.compile(r'<[^>]*>')


def _apply_collect(text: str, pattern, chunks: list) -> str:
    """移除匹配片段，并把被移除的内容收集到 ``chunks`` 中。

    :param text: 待处理文本
    :param pattern: 待移除的正则
    :param chunks: 收集被移除片段的列表
    :return: 移除后的文本
    """

    def _repl(match):
        chunks.append(match.group(0))
        return ''

    return pattern.sub(_repl, text)


def _reasoning_passes(text: str):
    """执行一次思维链剥离，返回 ``(剥离后文本, 思维链片段列表)``。

    剥离逻辑与 :func:`strip_reasoning` 完全一致，同时收集被移除的片段，
    供 :func:`extract_reasoning` 记录日志使用。

    :param text: 模型返回的原始文本
    :return: (剥离后文本, 思维链片段列表)
    """
    if not text:
        return text, []
    chunks: list = []
    for pattern in _REASONING_BLOCK_RES:
        text = _apply_collect(text, pattern, chunks)
    text = _apply_collect(text, _REASONING_SPECIAL_RE, chunks)
    for pattern in _REASONING_OPEN_RES:
        text = _apply_collect(text, pattern, chunks)
    text = _apply_collect(text, _REASONING_SPECIAL_OPEN_RE, chunks)
    # 孤儿闭标签：之后有正文时，其前内容视为思维链一并移除；否则仅移除标签
    for pattern in (_ORPHAN_CLOSE_RE, _ORPHAN_CLOSE_DS_RE):
        while True:
            match = pattern.search(text)
            if match is None:
                break
            after = text[match.end():]
            if after.strip():
                chunks.append(text[:match.end()])
                text = after
            else:
                chunks.append(match.group(0))
                text = pattern.sub('', text)
                break
    return text, chunks


def strip_reasoning(text: str) -> str:
    """移除推理模型的思维链片段，仅保留对用户可见的回答。

    :param text: 模型返回的原始文本
    :return: 移除思维链后的文本
    """
    if not text:
        return text
    result, _ = _reasoning_passes(text)
    result = result.strip()
    if result != text:
        _log.debug(f'已移除思维链片段（{len(text) - len(result)} 字）')
    return result


def extract_reasoning(text: str) -> str:
    """提取推理模型返回文本中的思维链内容（仅用于日志，不发送给用户）。

    :param text: 模型返回的原始文本
    :return: 去除标签后的思维链文本（无思维链时为空串）
    """
    if not text:
        return ''
    _, chunks = _reasoning_passes(text)
    cleaned = []
    for chunk in chunks:
        # 去掉 <think> / </think> / DeepSeek 全角分隔符等标签，仅保留思考内容
        inner = _TAG_STRIP_RE.sub(' ', chunk).strip()
        if inner:
            cleaned.append(inner)
    return '\n'.join(cleaned)
