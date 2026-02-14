# -*- coding: utf-8 -*-
import json
import os
import re

from ncatbot.utils.logger import get_log

_log = get_log("openai_chat_plugin.tools")

tools = [
    {
        "type": "function",
        "function": {
            "name": "memory_common_tool",
            "description": "用于读取、写入记忆数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["add", "query"],
                        "description": "操作类型，add表示添加记忆，query表示查询记忆"
                    },
                    "content": {
                        "type": "string",
                        "description": "当action为add时，content是要添加的记忆内容；当action为query时，content是查询的关键词或问题，支持正则表达式；不输入则查询全部记忆"
                    }
                },
                "required": ["action"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "memory_delete_tool",
            "description": "用于删除记忆数据",
            "parameters": {
                "type": "object",
                "properties": {
                    "id_list": {
                        "type": "array",
                        "items": {
                            "type": "integer"
                        },
                        "description": "要删除的记忆的ID"
                    }
                },
                "required": ["id_list"]
            }
        }
    }
]


def memory_common_tool(work_space: os.PathLike | str, action: str, content: str) -> str:
    """记忆读取、写入工具

    :param work_space: 工作空间对象或路径
    :param action: 操作类型，add表示添加记忆，query表示查询记忆
    :param content: 当action为add时，content是要添加的记忆内容；当action为query时，content是查询的关键词或问题，不输入则查询全部记忆
    :return: str, json字符串，包含操作结果
    """
    # 这里的记忆数据结构为一个列表，每条记忆是一个字典，包含id和content字段，例如：
    # [
    #     {"id": 1, "content": "这是第一条记忆"},
    #     {"id": 2, "content": "这是第二条记忆"}
    # ]

    _log.debug(f"执行记忆工具，操作类型: {action}, 内容: {content}")

    # 检查操作类型是否合法
    if action not in ["add", "query"]:
        return json.dumps({"status": "error", "message": "无效的操作类型"}, ensure_ascii=False)

    # 打开记忆文件
    memory_file = os.path.join(work_space, "memory.json")
    if not os.path.exists(memory_file):
        _log.debug("记忆文件不存在，创建新的记忆文件")
        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump([], f)

    # 读取现有记忆数据
    with open(memory_file, 'r', encoding='utf-8') as f:
        memory_data = json.load(f)

    # 根据操作类型执行相应的逻辑
    # 添加记忆：生成新的ID，添加到记忆数据中，并写回文件
    if action == "add":
        new_id = max([item["id"] for item in memory_data], default=0) + 1
        new_memory = {"id": new_id, "content": content}
        memory_data.append(new_memory)
        with open(memory_file, 'w', encoding='utf-8') as f:
            json.dump(memory_data, f, ensure_ascii=False, indent=2)
        return json.dumps({"status": "success", "message": "记忆添加成功", "id": new_id}, ensure_ascii=False)

    # 查询记忆：根据content过滤记忆数据，如果content为空则返回全部记忆
    elif action == "query":
        if content:
            # 支持正则表达式查询
            pattern = re.compile(content, re.IGNORECASE)
            filtered_memory = [item for item in memory_data if pattern.search(item["content"])]
        else:
            filtered_memory = memory_data
        return json.dumps({"status": "success", "message": "", "data": filtered_memory}, ensure_ascii=False)
    return json.dumps({"status": "error", "message": "无效的操作类型"}, ensure_ascii=False)


def memory_delete_tool(work_space: os.PathLike | str, id_list: list[int]) -> str:
    """记忆删除工具

    :param work_space: 工作空间对象或路径
    :param id_list: 要删除的记忆的ID
    :return: str, json字符串，包含操作结果
    """
    _log.debug(f"执行记忆删除工具，要删除的记忆ID: {id_list}")

    memory_file = os.path.join(work_space, "memory.json")
    if not os.path.exists(memory_file):
        return json.dumps({"status": "error", "message": "记忆文件不存在"}, ensure_ascii=False)

    memory_file_object = open(memory_file, 'r+', encoding='utf-8')
    memory_data = json.load(memory_file_object)

    # 根据ID过滤掉要删除的记忆
    new_memory_data = [item for item in memory_data if item["id"] not in id_list]
    if len(new_memory_data) == len(memory_data):  # 没有找到要删除的记忆
        return json.dumps({"status": "error", "message": "未找到要删除的记忆"}, ensure_ascii=False)
    else:  # 写回文件
        memory_file_object.seek(0)
        json.dump(new_memory_data, memory_file_object, ensure_ascii=False, indent=2)
        memory_file_object.truncate()
        memory_file_object.close()
    return json.dumps({"status": "success", "message": "记忆删除成功"}, ensure_ascii=False)
