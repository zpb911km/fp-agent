"""
核心工具模块 - 不可插件化的基础工具

这些工具必须保持直接绑定，作为系统的基础设施：
- bash: Shell 命令执行
- read_file: 文件读取
- write_file: 文件写入
- edit_file: 精确文本替换
"""

import subprocess
import tempfile
import os
from typing import Dict, Any


# ── 核心工具定义（OpenAI function calling schema） ──────────────────────

CORE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行一条 shell 命令并获取输出。支持任意 bash 语法，可管道/重定向。超时 300 秒。",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "要执行的 shell 命令"},
                },
                "required": ["command"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "读取文件内容，支持指定行范围",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "offset": {"type": "integer", "description": "起始行号（从 0 开始，不传则从头）"},
                    "limit": {"type": "integer", "description": "最多读取行数"},
                },
                "required": ["file_path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "创建新文件或覆盖已有文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "content": {"type": "string", "description": "文件内容"},
                },
                "required": ["file_path", "content"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "edit_file",
            "description": "对文件进行精确字符串替换（只替换首次出现的位置），用于修改而非覆盖",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "old_string": {"type": "string", "description": "需要被替换的已有文本（必须在文件中精确存在）"},
                    "new_string": {"type": "string", "description": "替换后的新文本"},
                },
                "required": ["file_path", "old_string", "new_string"],
            },
        },
    },
]


def get_core_definitions() -> list:
    """返回核心工具的 OpenAI schema 定义"""
    return CORE_TOOL_DEFINITIONS.copy()


def execute_core_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """执行核心工具"""
    
    if tool_name == "bash":
        command = params.get("command")
        if not command:
            raise ValueError("bash 工具需要 command 参数")
        
        result = subprocess.run(
            command,
            shell=True,
            capture_output=True,
            text=True,
            timeout=300
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n[stderr]\n{result.stderr}"
        
        return output
    
    elif tool_name == "read_file":
        file_path = params.get("file_path")
        assert isinstance(file_path, str), "read_file 需要 file_path 参数"
        
        offset = params.get("offset", 0)
        limit = params.get("limit")
        
        with open(file_path, 'r', encoding='utf-8') as f:
            lines = f.readlines()
        
        start = offset if isinstance(offset, int) and offset >= 0 else 0
        end = start + limit if isinstance(limit, int) else None
        
        return ''.join(lines[start:end])
    
    elif tool_name == "write_file":
        file_path = params.get("file_path")
        content = params.get("content")
        
        if not file_path or content is None:
            raise ValueError("write_file 需要 file_path 和 content 参数")
        
        # 确保目录存在
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(content)
        
        return f"✅ 文件已写入：{file_path}"
    
    elif tool_name == "edit_file":
        file_path = params.get("file_path")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        
        assert isinstance(file_path, str) and isinstance(old_string, str) and isinstance(new_string, str), \
            "edit_file 需要 file_path, old_string, new_string 三个字符串参数"
        
        with open(file_path, 'r', encoding='utf-8') as f:
            content = f.read()
        
        if old_string not in content:
            raise ValueError(f"未找到目标文本：{old_string[:50]}...")
        
        new_content = content.replace(old_string, new_string, 1)
        
        with open(file_path, 'w', encoding='utf-8') as f:
            f.write(new_content)
        
        return f"✅ 已替换文本（首次出现）"
    
    else:
        raise ValueError(f"未知核心工具：{tool_name}")
