"""
核心工具模块 — 不可插件化的基础工具（全异步版本）

这些工具必须保持直接绑定，作为系统的基础设施：
- bash: Shell 命令执行
- read_file: 文件读取（注册到哈希表，供编辑校验）
- write_file: 文件写入（自动注册哈希）
- edit_file: 纯字符串替换，通过文件哈希标识目标（必须 read_file 后操作）

设计要点：
- 工具声明采用单一数据源：ToolSpec/ParamSpec 同时驱动 OpenAI schema 生成与参数路由，
  杜绝"schema 与执行函数双份手写"导致的漂移。
- 文件哈希 = md5(路径 + 内容)，(path, content) 联合身份唯一：
  两个内容相同但路径不同的文件哈希必然不同，杜绝内容寻址冲突。
"""

import asyncio
import contextlib
import hashlib
import locale
import os
import re
import signal
import tempfile
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TypedDict

from fp_core.platform_utils import find_bash, is_windows

# ── OpenAI function calling schema 类型（精确声明，替代裸 dict） ──────────


class OpenAIParameter(TypedDict):
    """单个工具参数定义（ParamSpec.to_openai 的返回）"""

    type: str
    description: str


class OpenAIParameters(TypedDict):
    """工具参数容器（properties/required）"""

    type: str
    properties: dict[str, OpenAIParameter]
    required: list[str]


class OpenAIFunction(TypedDict):
    """OpenAI function calling 的 function 定义"""

    name: str
    description: str
    parameters: OpenAIParameters


class OpenAISchema(TypedDict):
    """OpenAI function calling 完整 schema（ToolSpec.to_openai_schema 的返回）"""

    type: str
    function: OpenAIFunction


# ── 辅助函数 ─────────────────────────────────────────────────────────────


def _compute_file_hash(file_path: str, content: str) -> str:
    """计算文件的短哈希（6 字符），用于陈旧编辑检测。

    将路径拼入内容哈希，形成 (path, content) 联合身份：
    - 两个内容相同但路径不同的文件 → 哈希不同（杜绝内容寻址冲突）
    - 同一文件内容变化 → 哈希变化（陈旧检测仍有效）
    """
    return hashlib.md5(f"{file_path}\n{content}".encode()).hexdigest()[:6]


# ── 文件注册表：hash → path ─────────────────────────────────────────────
# 只有 read_file 或 write_file 注册过的文件才能被 edit_file 修改。
# 编辑后旧 hash 自动替换为新 hash，保持注册表同步。

_file_registry: dict[str, str] = {}


# ── 工具规格：单一数据源（schema 与路由均由它生成） ─────────────────────


@dataclass
class ParamSpec:
    """工具参数的声明（单一数据源）"""

    name: str
    type: str
    required: bool
    description: str
    error_hint: str = ""  # 缺失时附加到报错的补救指引（校验层已知答案就应送到报错里）

    def to_openai(self) -> OpenAIParameter:
        return {"type": self.type, "description": self.description}


@dataclass
class ToolSpec:
    """工具声明：名称/描述/参数 + 执行处理器"""

    name: str
    description: str
    params: list[ParamSpec]
    handler: Callable[..., Awaitable[str]]

    def to_openai_schema(self) -> OpenAISchema:
        """从声明生成 OpenAI function calling schema"""
        properties = {p.name: p.to_openai() for p in self.params}
        required = [p.name for p in self.params if p.required]
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": {
                    "type": "object",
                    "properties": properties,
                    "required": required,
                },
            },
        }

    async def run(self, params: dict[str, object]) -> str:
        """从参数字典路由：校验必填项 → 提取参数 → 调用底层处理器。

        只传实际提供的参数：可选参数缺省时不强传 None，
        由 handler 的默认值兜底（如 bash 的 timeout=300 / force=False）。
        """
        missing = [p.name for p in self.params if p.required and p.name not in params]
        if missing:
            hint = next((p.error_hint for p in self.params if p.name == missing[0] and p.error_hint), "")
            msg = f"工具 {self.name} 缺少必填参数: {', '.join(missing)}"
            if hint:
                msg += f"\n{hint}"
            raise ValueError(msg)
        kwargs = {p.name: params[p.name] for p in self.params if p.name in params}
        return await self.handler(**kwargs)


# ── 工具执行函数 ─────────────────────────────────────────────────────────


async def _kill_process_group(proc: asyncio.subprocess.Process) -> None:
    """击杀整个进程组（shell 及其所有子进程），防止孤儿泄漏。

    必须配合 start_new_session=True 使用：子进程独立会话/进程组，
    killpg(proc.pid) 只杀该组，不波及宿主进程。
    """
    if proc.returncode is not None:
        return
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError) as e:
        # 进程组已不存在（恰好退出）→ 忽略；权限不足 → 退化为只杀主进程
        if isinstance(e, PermissionError):
            with contextlib.suppress(ProcessLookupError):
                proc.kill()


# ── bash 副作用检查 ─────────────────────────────────────────────────────
# 定位：提醒层而非安全层——防"无意识自伤/毁灭"，不防"有意执行"（force 可绕过）。
# 来源：反思记录 8/14 的 pkill 自杀坑（同一 bash 调用里 pkill 杀掉执行命令的 shell 自身）。
# 关键事实：pkill 默认不排除自身进程；bash 工具内 `bash -c <command>` 的 cmdline 含
# command 全文，因此 pkill -f <任何词> 几乎必然命中执行它的 shell——故 pkill 直接 BLOCK。

# BLOCK：毁灭级（数据/系统级破坏）或极高自伤风险，必须 force=true 才放行
#
# 命令位锚定：纯文本正则会误伤引用性内容（grep/文档字符串里写 pkill、echo "rm -rf /"
# 曾被整体拦截）。改为只匹配"出现在命令位"的命令词——行首、;&| 之后、$(`/反引号
# 之后，可带 sudo/time/env 类前缀。引号内的字面量不处于命令位 → 放行。
# 定位是提醒层而非安全层：漏报可由 force 流程兜底，误报却直接阻塞正常工作。
_CMD_PREFIX = r"(?:^|[;&|`(]|\$\{?[A-Za-z0-9_]*:)[ \t]*(?:(?:sudo|doas|time|nohup|env|command)[ \t]+)*"

_BLOCK_PATTERNS: list[tuple[str, str]] = [
    (
        rf"{_CMD_PREFIX}rm[ \t]+(?=\S*[rR])(?=\S*[fF])\S+[ \t]+(?:/(?:\*)?(?=$|[\s;&|`])|~+(?:/)?(?=$|[\s;&|`])|\.{{1,2}}/?(?=$|[\s;&|`]))",  # noqa: E501
        "rm -rf 根目录/家目录/当前目录",
    ),
    (rf"{_CMD_PREFIX}mkfs(\.\w+)?\b", "磁盘格式化 mkfs"),
    (rf"{_CMD_PREFIX}fdisk\b", "磁盘分区 fdisk"),
    (rf"{_CMD_PREFIX}parted\b", "磁盘分区 parted"),
    (rf"{_CMD_PREFIX}dd\b[^\n]*\bof=/dev/", "dd 直接写 /dev/ 设备"),
    (rf"{_CMD_PREFIX}(shutdown|reboot|poweroff|halt)\b", "关机/重启/停机"),
    (r":\(\s*\)\s*\{\s*:\s*\|", "fork 炸弹"),
    (rf"{_CMD_PREFIX}chmod[ \t]+-R[ \t]+777[ \t]+(/[\s;&|]*|/[*])", "chmod -R 777 根目录"),
    (rf"{_CMD_PREFIX}chown[ \t]+-R\b[^\n]*\s/[\s;&|]*$", "chown -R 整个根目录"),
    (rf"{_CMD_PREFIX}pkill\b", "pkill 不排除自身进程，bash 工具内执行极易杀死执行命令的 shell（曾真实发生）"),
]


def _mask_quoted(command: str) -> str:
    """把引号字面量替换为等长空格，使引号内文本不再参与命令位匹配。

    单引号→双引号两轮屏蔽（串内引号天然交替，简单交替即可覆盖常见形态）。
    长度保持不变，避免影响 ^ 锚定与后续偏移。
    """
    masked = re.sub(r"'[^']*'", lambda m: " " * len(m.group(0)), command)
    masked = re.sub(r'"[^"]*"', lambda m: " " * len(m.group(0)), masked)
    return masked


def _check_side_effect(command: str) -> str:
    """检查命令是否命中 BLOCK 规则。返回拦截原因；无风险返回空字符串。

    匹配采用命令位锚定（见 _CMD_PREFIX 注释）+ 引号字面量剥离，
    多行命令用 re.MULTILINE 让 ^ 匹配每行开头（兼容 heredoc/脚本片段写法）。

    设计取舍：只保留执行前拦截（BLOCK）——同步工具协议下事后提示无意义，
    且工具调用记录本身常驻上下文，透明可审计，故不设 WARN 层。
    """
    masked = _mask_quoted(command)
    for pat, reason in _BLOCK_PATTERNS:
        if re.search(pat, masked, re.MULTILINE):
            return reason
    return ""


async def _execute_bash(command: str, timeout: int = 300, force: bool = False) -> str:
    """异步执行 shell 命令。

    方案：stdout/stderr 重定向到临时文件而非 PIPE——
    - PIPE 的 wait()/communicate() 会隐式等待管道 EOF：`sleep 50 &` 后台进程
      继承管道写端，导致 bash 工具无谓阻塞（等后台进程结束）。
    - 文件重定向后 _pipes 为空，wait() 只等进程退出，后台命令即时返回；
      同时天然规避管道 64KB 死锁，大输出由文件承载。
    - 配合 start_new_session=True（独立进程组）+ killpg 击杀，SIGINT/超时
      时 shell 与子进程一并清理，不挂死、不泄漏。

    Args:
        command: 要执行的 shell 命令
        timeout: 超时秒数（1~3600，默认 300），长任务可调大
        force: 设为 true 跳过副作用检查（危险命令直接放行，确认风险后使用）
    """
    if not command:
        raise ValueError("bash 工具需要 command 参数")

    # ── 副作用检查（force 绕过） ──
    if not force:
        reason = _check_side_effect(command)
        if reason:
            return (
                f"⛔ 命令被安全检查拦截：{reason}\n"
                f"命令: {command}\n"
                f"若要强制执行，请重新调用并设置 force=true（有风险，请确认后使用）"
            )

    # ── timeout 参数化（clamp 1~3600） ──
    try:
        timeout = int(timeout)
    except (TypeError, ValueError):
        timeout = 300
    timeout = max(1, min(timeout, 3600))

    cmd_prefix = ""
    start_time = time.monotonic()
    with tempfile.TemporaryFile() as out_f, tempfile.TemporaryFile() as err_f:
        try:
            if is_windows():
                bash_path = find_bash()
                if bash_path:
                    proc = await asyncio.create_subprocess_exec(
                        bash_path,
                        "-c",
                        command,
                        stdout=out_f,
                        stderr=err_f,
                        start_new_session=True,
                    )
                else:
                    cmd = f"chcp 65001 >nul & {command}"
                    proc = await asyncio.create_subprocess_shell(
                        cmd,
                        stdout=out_f,
                        stderr=err_f,
                        start_new_session=True,
                    )
                    cmd_prefix = "[cmd.exe 回退] "
            else:
                # 显式用 bash 而非 create_subprocess_shell（后者走 /bin/sh，
                # Linux 上常为 dash：不支持 ${var:0:6}、[[ ]]、heredoc 差异等
                # bash 语法，曾导致部署脚本反复 Bad substitution 返工）
                proc = await asyncio.create_subprocess_exec(
                    find_bash() or "bash",
                    "-c",
                    command,
                    stdout=out_f,
                    stderr=err_f,
                    start_new_session=True,
                )

            try:
                returncode = await asyncio.wait_for(proc.wait(), timeout=timeout)
            except TimeoutError:
                await _kill_process_group(proc)
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                return f"错误：命令执行超时（{timeout}秒），可加大 timeout 参数重试"
            except (KeyboardInterrupt, asyncio.CancelledError):
                await _kill_process_group(proc)
                with contextlib.suppress(TimeoutError, asyncio.CancelledError):
                    await asyncio.wait_for(proc.wait(), timeout=2)
                raise

            duration = time.monotonic() - start_time
            out_f.seek(0)
            err_f.seek(0)
            output = out_f.read().decode("utf-8", errors="replace")
            stderr_text = err_f.read().decode("utf-8", errors="replace") if err_f else ""
            if stderr_text.strip():
                output = f"{output}\n[stderr]\n{stderr_text}" if output else f"[stderr]\n{stderr_text}"

            if cmd_prefix and output.strip():
                if "\ufffd" in output:
                    enc = locale.getpreferredencoding()
                    out_f.seek(0)
                    output = out_f.read().decode(enc, errors="replace")
                    if err_f:
                        err_f.seek(0)
                        stderr_text2 = err_f.read().decode(enc, errors="replace")
                        if stderr_text2.strip():
                            output += f"\n[stderr]\n{stderr_text2}"
                return cmd_prefix + output.lstrip()

            if returncode != 0:
                return f"❌ 命令执行失败（exit={returncode}，{duration:.1f}s）\n命令: {command}\n{output}"
            if len(output) < 3000:
                return output

            _fd, _path = tempfile.mkstemp(prefix="fp_bash_", suffix=".log")
            with os.fdopen(_fd, "w", encoding="utf-8") as _f:
                _f.write(output)

            # 结构化预览：头部 + 尾部 + stderr 摘要，让 LLM 一次拿到关键信息
            # （报错通常在尾部或 stderr，旧实现只给头部 200 字符，常漏掉真正要看的）
            total_chars = len(output)
            total_lines = output.count("\n") + 1
            head = output[:200]
            tail = output[-200:] if total_chars > 400 else ""

            summary = (
                f"✅ 命令执行成功（exit=0，{duration:.1f}s）\n"
                f"输出较长（{total_chars} 字符 / {total_lines} 行），已保存至 {_path}"
            )
            if stderr_text.strip():
                summary += f"\n⚠️ stderr 存在（{len(stderr_text)} 字符），开头：\n{stderr_text[:200]}"
            return (
                f"{summary}\n\n"
                f"── 头部 200 字符 ────────────────────────\n{head}\n"
                f"────────────────────────────────────────\n"
                f"── 尾部 200 字符 ────────────────────────\n{tail}\n"
                f"────────────────────────────────────────\n\n"
                f"需要完整内容 → read_file({_path!r})"
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
            file_hash = _compute_file_hash(file_path, full_content)
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
                hints: list[str] = []
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
    if not file_path:
        raise ValueError("write_file 需要 file_path 和 content 参数")

    try:
        loop = asyncio.get_running_loop()

        def _write():
            os.makedirs(os.path.dirname(file_path), exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(content)

        await loop.run_in_executor(None, _write)

        # 清理该路径的旧注册条目，避免失效哈希残留
        for old_hash in [h for h, p in list(_file_registry.items()) if p == file_path]:
            del _file_registry[old_hash]

        new_hash = _compute_file_hash(file_path, content)
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
    if not file_hash or not old_string or not new_string:
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
            current_hash = _compute_file_hash(file_path, content)
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
            new_hash = _compute_file_hash(file_path, new_content)
            _file_registry[new_hash] = file_path

            return f"✅ 文件已修改: {file_path}\n  新哈希: {new_hash}"

        return await loop.run_in_executor(None, _edit)
    except Exception as e:
        return f"错误：{e}"


# ── 工具声明（单一数据源） ───────────────────────────────────────────────

CORE_TOOLS: list[ToolSpec] = [
    ToolSpec(
        name="bash",
        description="执行 shell 命令。小输出直接返回，大输出(≥3K)自动保存文件+返回预览。"
        "默认超时 300 秒，可用 timeout 参数调整。危险命令（rm -rf 根目录、pkill、磁盘操作、关机等）"
        "会被安全检查拦截，确认风险后可用 force=true 强制执行。",
        params=[
            ParamSpec("command", "string", True, "要执行的 shell 命令"),
            ParamSpec("timeout", "integer", False, "超时秒数（1~3600，默认 300），长任务可调大"),
            ParamSpec("force", "boolean", False, "设为 true 跳过副作用检查（危险命令放行，确认风险后使用）"),
        ],
        handler=_execute_bash,
    ),
    ToolSpec(
        name="read_file",
        description="读取文件内容。默认返回前 200 行（limit=200），超出提示继续读取。"
        "末尾附带文件哈希，供 edit_file 的 file_hash 参数做陈旧检测。",
        params=[
            ParamSpec("file_path", "string", True, "文件绝对路径"),
            ParamSpec("offset", "integer", False, "起始行号（从 0 开始，不传则从头）"),
            ParamSpec("limit", "integer", False, "最多读取行数（默认 200，上限 500）"),
        ],
        handler=_execute_read_file,
    ),
    ToolSpec(
        name="write_file",
        description="创建新文件或覆盖已有文件",
        params=[
            ParamSpec("file_path", "string", True, "文件绝对路径"),
            ParamSpec("content", "string", True, "文件内容"),
        ],
        handler=_execute_write_file,
    ),
    ToolSpec(
        name="edit_file",
        description="通过文件哈希标识目标文件，对文件进行精确字符串替换。\n"
        "流程：① read_file 读取 → ② 记录返回的【文件哈希】→ ③ 传入该哈希 + old_string + new_string 编辑。\n"
        "安全：只有 read_file 或 write_file 注册过的文件才能被编辑，\n"
        "哈希不匹配自动拒绝（文件已被外部修改，需重新读取）。",
        params=[
            ParamSpec(
                "file_hash",
                "string",
                True,
                "文件的哈希标识（6字符，read_file 末尾返回的【文件哈希】）。"
                "必需：只有此 hash 在文件注册表中存在且匹配当前文件内容时编辑才生效。",
                error_hint="补救：先 read_file 目标文件，取其末尾【文件哈希】作为 file_hash 重试。",
            ),
            ParamSpec(
                "old_string",
                "string",
                True,
                "需要被替换的已有文本，必须与文件中内容完全一致（含缩进）。",
            ),
            ParamSpec(
                "new_string",
                "string",
                True,
                "替换后的新内容。",
            ),
        ],
        handler=_execute_edit_file,
    ),
]

_TOOL_INDEX: dict[str, ToolSpec] = {t.name: t for t in CORE_TOOLS}


def get_core_definitions() -> list[OpenAISchema]:
    """返回核心工具的 OpenAI schema 定义（由 ToolSpec 单一数据源生成）"""
    return [t.to_openai_schema() for t in CORE_TOOLS]


async def execute_core_tool(tool_name: str, params: dict[str, object]) -> str:
    """
    执行核心工具（异步）

    Args:
        tool_name: bash / read_file / write_file / edit_file
        params: 参数字典

    Returns:
        执行结果
    """
    spec = _TOOL_INDEX.get(tool_name)
    if spec is None:
        raise ValueError(f"未知核心工具：{tool_name}")
    return await spec.run(params)
