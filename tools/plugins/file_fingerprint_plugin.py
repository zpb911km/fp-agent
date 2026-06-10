"""
File Fingerprint 插件 — 文件指纹识别（异步版本）

使用 `file` 和 `strings` 命令提取文件特征，判断类型。
"""

import asyncio
from typing import Any

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


async def execute(params: dict[str, Any]) -> str:
    """
    文件指纹识别（异步）

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
        proc1 = await asyncio.create_subprocess_exec(
            "file",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout1, _ = await proc1.communicate()
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc1.kill()
            await proc1.wait()
            raise
        file_info = stdout1.decode("utf-8", errors="replace").strip()

        # strings 命令（前 20 行）
        proc2 = await asyncio.create_subprocess_exec(
            "strings",
            file_path,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout2, stderr2 = await proc2.communicate()
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc2.kill()
            await proc2.wait()
            raise
        strings_lines = stdout2.decode("utf-8", errors="replace").split("\n")[:20]
        strings_info = "\n".join(strings_lines)

        output = f"file:\n{file_info}\n\nstrings (前20行):\n{strings_info}"

        stderr_text = stderr2.decode("utf-8", errors="replace").strip()
        if stderr_text:
            output += f"\n\n[stderr]\n{stderr_text[:500]}"

        return output

    except FileNotFoundError:
        return "错误：文件不存在"
    except Exception as e:
        return f"错误：{e}"
