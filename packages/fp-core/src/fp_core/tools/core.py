"""
核心工具模块 — 不可插件化的基础工具（全异步版本）

这些工具必须保持直接绑定，作为系统的基础设施：
- bash: Shell 命令执行
- read_file: 文件读取（注册到哈希表，供编辑校验）
- write_file: 文件写入（自动注册哈希）
- edit_file: 纯字符串替换，通过文件哈希标识目标（必须 read_file 后操作）
"""

import asyncio
import contextlib
import glob
import hashlib
import locale
import os
import tempfile
import time
from typing import Any

from fp_core.platform_utils import find_bash, is_windows

# ── 辅助函数 ─────────────────────────────────────────────────────────────


def _compute_file_hash(content: str) -> str:
    """计算文件内容的短哈希（6 字符），用于陈旧编辑检测"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()[:6]


# ── 文件注册表：hash → path ─────────────────────────────────────────────
# 只有 read_file 或 write_file 注册过的文件才能被 edit_file 修改。
# 编辑后旧 hash 自动替换为新 hash，保持注册表同步。

_file_registry: dict[str, str] = {}


# ── 核心工具定义（OpenAI function calling schema） ──────────────────────

CORE_TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "bash",
            "description": "执行 shell 命令。小输出直接返回，大输出(≥3K)自动保存文件+返回预览。超时 300 秒。",
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
            "description": "读取文件内容。默认返回前 200 行（limit=200），超出提示继续读取。"
            "末尾附带文件哈希，供 edit_file 的 file_hash 参数做陈旧检测。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_path": {"type": "string", "description": "文件绝对路径"},
                    "offset": {"type": "integer", "description": "起始行号（从 0 开始，不传则从头）"},
                    "limit": {"type": "integer", "description": "最多读取行数（默认 200，上限 500）"},
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
            "description": "通过文件哈希标识目标文件，对文件进行精确字符串替换。\n"
            "流程：① read_file 读取 → ② 记录返回的【文件哈希】→ ③ 传入该哈希 + old_string + new_string 编辑。\n"
            "安全：只有 read_file 或 write_file 注册过的文件才能被编辑，\n"
            "哈希不匹配自动拒绝（文件已被外部修改，需重新读取）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "file_hash": {
                        "type": "string",
                        "description": "文件的哈希标识（6字符，read_file 末尾返回的【文件哈希】）。"
                        "必需：只有此 hash 在文件注册表中存在且匹配当前文件内容时编辑才生效。",
                    },
                    "old_string": {
                        "type": "string",
                        "description": "需要被替换的已有文本，必须与文件中内容完全一致（含缩进）。",
                    },
                    "new_string": {
                        "type": "string",
                        "description": "替换后的新内容。",
                    },
                },
                "required": ["file_hash", "old_string", "new_string"],
            },
        },
    },
]


def get_core_definitions() -> list:
    """返回核心工具的 OpenAI schema 定义"""
    return list(CORE_TOOL_DEFINITIONS)


# ── 工具执行函数 ─────────────────────────────────────────────────────────


async def _execute_bash(command: str) -> str:
    """异步执行 shell 命令"""
    if not command:
        raise ValueError("bash 工具需要 command 参数")

    tmpdir = tempfile.gettempdir()
    for f in glob.glob(os.path.join(tmpdir, "fp_bash_*.log")):
        with contextlib.suppress(OSError):
            os.unlink(f)

    cmd_prefix = ""
    start_time = time.monotonic()
    try:
        if is_windows():
            bash_path = find_bash()
            if bash_path:
                proc = await asyncio.create_subprocess_exec(
                    bash_path,
                    "-c",
                    command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
            else:
                cmd = f"chcp 65001 >nul & {command}"
                proc = await asyncio.create_subprocess_shell(
                    cmd,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                )
                cmd_prefix = "[cmd.exe 回退] "
        else:
            proc = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=300)
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return "错误：命令执行超时（300秒）"
        except (KeyboardInterrupt, asyncio.CancelledError):
            proc.kill()
            await proc.wait()
            raise

        duration = time.monotonic() - start_time
        output = stdout.decode("utf-8", errors="replace")
        stderr_text = stderr.decode("utf-8", errors="replace") if stderr else ""
        if stderr_text.strip():
            output = f"{output}\n[stderr]\n{stderr_text}" if output else f"[stderr]\n{stderr_text}"

        if cmd_prefix and output.strip():
            if "\ufffd" in output:
                enc = locale.getpreferredencoding()
                output = stdout.decode(enc, errors="replace")
                if stderr:
                    stderr_text2 = stderr.decode(enc, errors="replace")
                    if stderr_text2.strip():
                        output += f"\n[stderr]\n{stderr_text2}"
            return cmd_prefix + output.lstrip()

        if proc.returncode != 0:
            return f"❌ 命令执行失败（exit={proc.returncode}，{duration:.1f}s）\n命令: {command}\n{output}"
        if len(output) < 3000:
            return output

        _fd, _path = tempfile.mkstemp(prefix="fp_bash_", suffix=".log")
        with os.fdopen(_fd, "w", encoding="utf-8") as _f:
            _f.write(output)
        preview = output[:200]
        return (
            f"✅ 命令执行成功（exit=0，{duration:.1f}s）\n"
            f"输出较长（{len(output)} 字符），已保存至 {_path}\n\n"
            f"前 200 字符预览：\n────────────────────────\n{preview}\n"
            f"────────────────────────\n\n需要完整内容 → read_file({_path!r})"
        )
    except Exception as e:
        return f"错误：{e}"


async def _execute_read_file(file_path: str, offset: int | None = None, limit: int | None = None) -> str:
    """异步读取文件。末尾附带文件哈希（6字符），供 edit_file 陈旧检测。"""
    if not file_path:
        raise ValueError("read_file 需要 file_path 参数")

    try:
        loop = asyncio.get_running_loop()

        def _read():
            with open(file_path, encoding="utf-8") as f:
                all_lines = f.readlines()
            total_lines = len(all_lines)
            full_content = "".join(all_lines)
            file_hash = _compute_file_hash(full_content)
            _file_registry[file_hash] = file_path

            lines = all_lines[offset:] if offset else list(all_lines)
            effective_limit = limit if limit is not None else 200
            if effective_limit > 500:
                effective_limit = 500

            content_lines = lines[:effective_limit]
            content = "".join(content_lines)
            lines_returned = len(content_lines)

            char_cut = False
            if len(content) > 10000:
                content = content[:10000]
                char_cut = True

            remainder = total_lines - (offset or 0) - lines_returned
            if offset and offset > total_lines:
                return f"错误：offset={offset} 超出文件总行数 ({total_lines})"

            if (remainder > 0) or char_cut:
                next_offset = (offset or 0) + lines_returned
                hints = []
                if remainder > 0:
                    hints.append(f"共 {total_lines} 行，已读 {lines_returned} 行，剩余 {remainder} 行")
                if char_cut:
                    hints.append("已截断至 10000 字符")
                content += (
                    f"\n--- 文件已截断（{'；'.join(hints)}）"
                    f"\n继续读取：read_file(file_path={file_path!r}, offset={next_offset}, limit=200)"
                )

            content += f"\n\n【文件哈希】{file_hash}"
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
        new_hash = _compute_file_hash(content)
        _file_registry[new_hash] = file_path
        return f"文件已写入: {file_path}  哈希: {new_hash}"
    except Exception as e:
        return f"错误：{e}"


async def _execute_edit_file(
    file_hash: str,
    old_string: str,
    new_string: str,
) -> str:
    """
    通过文件哈希标识目标文件，进行精确字符串替换。

    流程：
      1. 通过 file_hash 在注册表中查找对应文件路径
      2. 读取文件，验证当前哈希是否匹配（防止陈旧编辑）
      3. 精确替换 old_string → new_string（只替换首次出现）
      4. 更新注册表：旧 hash 移除，新 hash 入册

    Args:
        file_hash: 文件哈希标识（6字符，read_file 返回的【文件哈希】）
        old_string: 需要被替换的已有文本
        new_string: 替换后的新文本
    """
    if not file_hash or old_string is None or new_string is None:
        raise ValueError("edit_file 需要 file_hash, old_string, new_string 参数")

    try:
        loop = asyncio.get_running_loop()

        def _edit() -> str:
            # 从注册表查找路径
            file_path = _file_registry.get(file_hash)
            if file_path is None:
                return (
                    f"错误：哈希 {file_hash!r} 未在文件注册表中找到\n"
                    f"  请先用 read_file 读取目标文件以注册，或先用 write_file 写入文件。"
                )

            # 读取文件
            try:
                with open(file_path, encoding="utf-8") as f:
                    content = f.read()
            except FileNotFoundError:
                _file_registry.pop(file_hash, None)
                return f"错误：文件不存在 {file_path}（已从注册表移除）"

            # 哈希校验（陈旧检测）
            current_hash = _compute_file_hash(content)
            if current_hash != file_hash:
                _file_registry.pop(file_hash, None)
                if current_hash in _file_registry:
                    _file_registry.pop(current_hash, None)
                return (
                    f"❌ 编辑被拒绝：文件哈希不匹配\n"
                    f"  期望: {file_hash}  实际: {current_hash}\n"
                    f"  文件自上次读取后已被修改，请重新 read_file 后重试。"
                )

            # 字符串替换（精确匹配，只替换首次出现）
            if old_string not in content:
                return "错误：未找到要替换的文本。请确保 old_string 与文件中内容完全一致（含缩进）。"
            if content.count(old_string) > 1:
                return "错误：存在多个匹配项，请指定更加明确的 old_string 参数（增加前后文以唯一匹配）。"

            new_content = content.replace(old_string, new_string, 1)

            # 写入
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)

            # 更新注册表
            _file_registry.pop(file_hash, None)
            new_hash = _compute_file_hash(new_content)
            _file_registry[new_hash] = file_path

            return f"✅ 文件已修改: {file_path}\n  新哈希: {new_hash}"

        return await loop.run_in_executor(None, _edit)
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
            file_hash=params.get("file_hash", ""),
            old_string=params.get("old_string", ""),
            new_string=params.get("new_string", ""),
        )
    else:
        raise ValueError(f"未知核心工具：{tool_name}")
