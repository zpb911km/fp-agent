"""
核心工具模块 — 不可插件化的基础工具（全异步版本）

这些工具必须保持直接绑定，作为系统的基础设施：
- bash: Shell 命令执行（跨平台：Linux 用 bash，Windows 用 Git Bash）
- read_file: 文件读取
- write_file: 文件写入
- edit_file: 精确文本替换
"""

import asyncio
import os
from typing import Any

from fp_core.platform_utils import find_bash, is_windows

# ── 核心工具定义（OpenAI function calling schema） ──────────────────────

CORE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": (
                "执行 shell 命令，支持管道/重定向。"
                "跨平台：Linux 原生执行，Windows 自动路由到 Git Bash 或降级 cmd.exe。"
                "超时 300 秒。"
            ),
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


async def _execute_bash(command: str) -> str:
    """异步执行 shell 命令

    跨平台策略：
      - Linux/macOS:      使用 asyncio.create_subprocess_shell，由系统 shell 执行
      - Windows + Git Bash: 使用 bash.exe -c，Unix 命令（ls/grep/awk）可用
      - Windows + 无 Git Bash: 降级到 cmd.exe /c，Windows 命令（dir/type/findstr）可用
    """
    if not command:
        raise ValueError("bash 工具需要 command 参数")

    # ── 跨平台路由：选择正确的 shell 执行方式 ──
    cmd_prefix = ""  # 输出前缀，cmd.exe 回退时标记
    try:
        if is_windows():
            bash_path = find_bash()
            if bash_path:
                # Git Bash 可用 → 走 bash.exe，Unix 命令
                proc = await asyncio.create_subprocess_exec(
                    bash_path,
                    "-c",
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                # Git Bash 不可用 → 降级到 cmd.exe，Windows 命令
                proc = await asyncio.create_subprocess_shell(
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                cmd_prefix = "[cmd.exe 回退] "
        else:
            # Linux/macOS → 系统 shell
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(),
                timeout=300,
            )
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "错误：命令执行超时（300秒）"
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise

        output = stdout.decode("utf-8", errors="replace")
        if stderr:
            stderr_text = stderr.decode("utf-8", errors="replace")
            if stderr_text.strip():
                output += f"\n[stderr]\n{stderr_text}"

        # 截断 10000 字符
        if len(output) > 10000:
            output = output[:10000] + f"\n...（已截断，原文 {len(output)} 字符）"

        # cmd.exe 回退模式下加前缀告知 LLM
        if cmd_prefix and output.strip():
            output = cmd_prefix + output.lstrip()

        return output
    except Exception as e:
        return f"错误：{e}"


async def _execute_read_file(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    """异步读取文件"""
    if not file_path:
        raise ValueError("read_file 需要 file_path 参数")

    try:
        loop = asyncio.get_running_loop()

        def _read():
            with open(file_path, encoding="utf-8") as f:
                if offset:
                    for _ in range(offset):
                        f.readline()

                content = f.read()
                if limit:
                    lines = content.split("\n")
                    return "\n".join(lines[:limit])
                return content

        return await loop.run_in_executor(None, _read)
    except FileNotFoundError:
        return f"错误：文件不存在 {file_path}"
    except Exception as e:
        return f"错误：{e}"


async def _execute_write_file(file_path: str, content: str) -> str:
    """异步写入文件"""
    if not file_path or content is None:
        raise ValueError("write_file 需要 file_path 和 content 参数")

    try:
        loop = asyncio.get_running_loop()

        def _write():
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

        await loop.run_in_executor(None, _write)
        return f"文件已写入: {file_path}"
    except Exception as e:
        return f"错误：{e}"


async def _execute_edit_file(file_path: str, old_string: str, new_string: str) -> str:
    """异步编辑文件"""
    if not file_path or old_string is None or new_string is None:
        raise ValueError("edit_file 需要 file_path, old_string, new_string 参数")

    try:
        loop = asyncio.get_running_loop()

        def _edit():
            with open(file_path, encoding="utf-8") as f:
                content = f.read()

            if old_string not in content:
                return "错误：未找到要替换的文本"

            # 检查唯一性
            if content.count(old_string) > 1:
                return "错误：存在多个匹配项，请指定更加明确的 old_string 参数"

            content = content.replace(old_string, new_string, 1)

            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

            return f"文件已修改: {file_path}"

        return await loop.run_in_executor(None, _edit)
    except FileNotFoundError:
        return f"错误：文件不存在 {file_path}"
    except Exception as e:
        return f"错误：{e}"


async def execute_core_tool(tool_name: str, params: dict[str, Any]) -> Any:
    """
    执行核心工具（异步）

    Args:
        tool_name: bash / read_file / write_file / edit_file
        params: 参数字典

    Returns:
        执行结果
    """
    if tool_name == "bash":
        return await _execute_bash(params.get("command", ""))
    elif tool_name == "read_file":
        return await _execute_read_file(
            params.get("file_path", ""),
            params.get("offset"),
            params.get("limit"),
        )
    elif tool_name == "write_file":
        return await _execute_write_file(
            params.get("file_path", ""),
            params.get("content", ""),
        )
    elif tool_name == "edit_file":
        return await _execute_edit_file(
            params.get("file_path", ""),
            params.get("old_string", ""),
            params.get("new_string", ""),
        )
    else:
        raise ValueError(f"未知核心工具：{tool_name}")
