"""
File Fingerprint 插件 — 文件指纹识别

使用 `file` 和 `strings` 命令提取文件特征，判断类型。
"""

import subprocess
from typing import Any, Dict


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "file_fingerprint",
        "description": "文件指纹识别 - 使用 file 和 strings 命令提取特征，判断类型",
        "parameters": {
            "type": "object",
            "properties": {
                "file_path": {"type": "string", "description": "文件路径"},
            },
            "required": ["file_path"],
        },
    },
}


def execute(params: Dict[str, Any]) -> str:
    """
    文件指纹识别
    
    Args:
        params: 包含 file_path 的字典
        
    Returns:
        file 和 strings 命令的输出
    """
    file_path = params.get("file_path", "")
    if not file_path:
        raise ValueError("file_fingerprint 需要 file_path 参数")
    
    try:
        # file 命令
        result = subprocess.run(["file", file_path], capture_output=True, text=True)
        file_info = result.stdout.strip()
        
        # strings 命令（前 20 行）
        result2 = subprocess.run(
            ["strings", file_path],
            capture_output=True,
            text=True,
        )
        strings_lines = result2.stdout.split("\n")[:20]
        strings_info = "\n".join(strings_lines)
        
        output = f"file:\n{file_info}\n\nstrings (前20行):\n{strings_info}"
        
        if result2.stderr:
            output += f"\n\n[stderr]\n{result2.stderr[:500]}"
        
        return output
    
    except FileNotFoundError:
        return "错误：文件不存在"
    except Exception as e:
        return f"错误：{e}"
