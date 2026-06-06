"""
Python 插件 — 执行 Python 代码

通过临时文件方式执行用户提供的 Python 代码，适合复杂数据处理和算法验证。
"""

import os
import subprocess
import tempfile
from typing import Any, Dict


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "python",
        "description": "执行一段 Python 代码（通过临时文件），适合复杂数据处理、算法验证等。超时 30 秒。",
        "parameters": {
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
            },
            "required": ["code"],
        },
    },
}


def execute(params: Dict[str, Any]) -> str:
    """
    执行 Python 代码
    
    Args:
        params: 包含 'code' 键的字典
        
    Returns:
        代码执行输出
    """
    code = params.get("code", "")
    if not code:
        raise ValueError("python 插件需要 code 参数")
    
    tmp_path = None
    try:
        # 写入临时文件
        with tempfile.NamedTemporaryFile(
            mode="w",
            suffix=".py",
            prefix="agent_python_",
            delete=False,
            encoding="utf-8",
        ) as f:
            f.write(code)
            tmp_path = f.name
        
        # 执行
        result = subprocess.run(
            ["python3", tmp_path],
            capture_output=True,
            text=True,
            timeout=30,
        )
        
        output = result.stdout
        if result.stderr:
            if output:
                output += "\n"
            output += result.stderr
        
        # 截断
        if len(output) > 5000:
            output = output[:5000] + f"\n...（已截断，原文 {len(output)} 字符）"
        
        return output if output else "（无输出）"
    
    except subprocess.TimeoutExpired:
        return "错误：代码执行超时（30秒）"
    except Exception as e:
        return f"错误：{e}"
    finally:
        # 清理临时文件
        if tmp_path and os.path.exists(tmp_path):
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
