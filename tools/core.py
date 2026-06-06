"""
核心工具模块 — 不可插件化的基础工具

这些工具必须保持直接绑定，作为系统的基础设施：
- bash: Shell 命令执行
- read_file: 文件读取
- write_file: 文件写入
- edit_file: 精确文本替换
"""

import os
import subprocess
from typing import Any, Dict


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
    return list(CORE_TOOL_DEFINITIONS)


def execute_core_tool(tool_name: str, params: Dict[str, Any]) -> Any:
    """
    执行核心工具
    
    Args:
        tool_name: bash / read_file / write_file / edit_file
        params: 参数字典
        
    Returns:
        执行结果
    """
    if tool_name == "bash":
        command = params.get("command")
        if not command:
            raise ValueError("bash 工具需要 command 参数")
        
        try:
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
            
            # 截断 10000 字符
            if len(output) > 10000:
                output = output[:10000] + f"\n...（已截断，原文 {len(output)} 字符）"
            
            return output
        except subprocess.TimeoutExpired:
            return "错误：命令执行超时（300秒）"
        except Exception as e:
            return f"错误：{e}"
    
    elif tool_name == "read_file":
        file_path = params.get("file_path")
        if not file_path:
            raise ValueError("read_file 需要 file_path 参数")
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                offset = params.get("offset", 0)
                limit = params.get("limit")
                
                if offset:
                    for _ in range(offset):
                        f.readline()
                
                content = f.read()
                if limit:
                    lines = content.split("\n")
                    return "\n".join(lines[:limit])
                return content
        except FileNotFoundError:
            return f"错误：文件不存在 {file_path}"
        except Exception as e:
            return f"错误：{e}"
    
    elif tool_name == "write_file":
        file_path = params.get("file_path")
        content = params.get("content")
        
        if not file_path or content is None:
            raise ValueError("write_file 需要 file_path 和 content 参数")
        
        try:
            # 确保目录存在
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            
            return f"文件已写入: {file_path}"
        except Exception as e:
            return f"错误：{e}"
    
    elif tool_name == "edit_file":
        file_path = params.get("file_path")
        old_string = params.get("old_string")
        new_string = params.get("new_string")
        
        if not file_path or old_string is None or new_string is None:
            raise ValueError("edit_file 需要 file_path, old_string, new_string 参数")
        
        try:
            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
            
            if old_string not in content:
                return f"错误：未找到要替换的文本"
            
            # 检查唯一性
            if content.count(old_string) > 1:
                return f"错误：存在多个匹配项，请指定更加明确的 old_string 参数"
            
            content = content.replace(old_string, new_string, 1)
            
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)
            
            return f"文件已修改: {file_path}"
        except FileNotFoundError:
            return f"错误：文件不存在 {file_path}"
        except Exception as e:
            return f"错误：{e}"
    
    else:
        raise ValueError(f"未知核心工具：{tool_name}")
