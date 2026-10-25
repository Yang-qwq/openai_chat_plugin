# -*- coding: utf-8 -*-
"""思维链剥离纯函数测试（不加载框架插件，仅导入 reasoning.py）。

移植自 NcatBotTRPG 的 tests/test_reasoning_unit.py（AGPL-3.0，同作者）。

运行：python -m pytest tests -v -o "addopts="
"""
import sys
from pathlib import Path

import pytest

# 插件根目录（reasoning.py 所在目录）加入 sys.path，便于直接导入纯函数
PLUGIN_DIR = Path(__file__).resolve().parents[1]
if str(PLUGIN_DIR) not in sys.path:
    sys.path.insert(0, str(PLUGIN_DIR))

from reasoning import extract_reasoning, strip_reasoning  # noqa: E402

# DeepSeek-R1 风格全角分隔符
_DS_BEGIN = '<｜begin▁of▁thinking｜>'
_DS_END = '<｜end▁of▁thinking｜>'


@pytest.mark.parametrize('raw, expected', [
    ('<thinking>内幕推理</thinking>你好', '你好'),
    ('<think>分析</think>答案', '答案'),
    ('<reasoning>推理过程</reasoning>\n\n结果', '结果'),
    ('<analysis>分析</analysis>输出', '输出'),
    ('<thought>思考</thought>回复', '回复'),
    ('你好<thinking>未闭合的思维链', '你好'),      # 未闭合：连同其后内容移除
    ('<think>思考中</think>', ''),                    # 仅思维链
    (f'{_DS_BEGIN}推理{_DS_END}答复', '答复'),      # DeepSeek 全角分隔符
    ('推理内容</think>回答', '回答'),               # 孤儿闭标签：其前视为思维链
    ('</think>回答', '回答'),                       # 开标签已被剥离，只剩闭标签
    ('回答</think>', '回答'),                       # 闭标签位于结尾：仅移除标签
    ('推理</think>回答</think>', '回答'),           # 多个孤儿闭标签
    (f'推理{_DS_END}答复', '答复'),                 # DeepSeek 孤儿闭标签
    (f'答复{_DS_END}', '答复'),                     # DeepSeek 闭标签在结尾
    ('</thinking>你好', '你好'),                    # thinking 孤儿闭标签
    ('普通回答，无思维链', '普通回答，无思维链'),
    ('', ''),
])
def test_strip_reasoning(raw, expected):
    assert strip_reasoning(raw) == expected


@pytest.mark.parametrize('raw, expected', [
    ('<thinking>内幕推理</thinking>你好', '内幕推理'),
    ('<think>分析</think>答案', '分析'),
    ('<reasoning>推理过程</reasoning>结果', '推理过程'),
    ('<think>先分析</think><reasoning>再推理</reasoning>回答', '先分析\n再推理'),
    ('推理内容</think>回答', '推理内容'),           # 孤儿闭标签：其前为思维链
    ('</think>回答', ''),                           # 仅闭标签，无思考内容
    ('回答</think>', ''),                           # 闭标签在结尾，其前为正文
    ('你好<thinking>未闭合的思维链', '未闭合的思维链'),
    (f'{_DS_BEGIN}推理{_DS_END}答复', '推理'),      # DeepSeek 全角分隔符
    ('普通回答，无思维链', ''),
    ('', ''),
])
def test_extract_reasoning(raw, expected):
    assert extract_reasoning(raw) == expected
