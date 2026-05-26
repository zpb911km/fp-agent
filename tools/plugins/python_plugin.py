"""
Python 插件 - 执行 Python 代码

通过临时文件方式执行用户提供的 Python 代码，适合复杂数据处理和算法验证。
"""

import subprocess
import tempfile
import os


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


def execute(params: dict) -> str:
    """
    执行 Python 代码
    
    Args:
        params: 包含 'code' 键的字典
        
    Returns:
        执行结果字符串
    """
    code = params.get("code")
    
    if not code:
        raise ValueError("python 插件需要 code 参数")
    
    # 创建临时文件
    with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
        f.write(code)
        temp_path = f.name
    
    try:
        # 执行 Python 代码
        result = subprocess.run(
            ['python3', temp_path],
            capture_output=True,
            text=True,
            timeout=30
        )
        
        output = result.stdout
        if result.stderr:
            output += f"\n[错误]\n{result.stderr}"
        
        return output if output else "[无输出]"
        
    finally:
        # 清理临时文件
        os.unlink(temp_path)
