"""
ACP Server — Agent Client Protocol 实现 (v1 兼容)
==================================================

将 Five Pebbles 作为 ACP Server 接入 IDE（Zed / VS Code / JetBrains 等）。

协议规范：https://agentclientprotocol.com/protocol/v1
参考实现：https://github.com/agentclientprotocol/agent-client-protocol

架构：
  IDE (ACP Client)
      │  stdio (JSON-RPC 2.0, 一行一条 JSON)
      ▼
  ACP Server（本文件）
      │  redirect_stdout → stderr（日志/display）
      ▼
  Agent.process()   ← 五块卵石核心

关键修正 (v1)：
  - protocolVersion: 1 (integer, 不是字符串)
  - 字段名使用 camelCase: agentCapabilities, agentInfo, sessionId
  - session/prompt 使用 prompt 数组 + 向后兼容 messages
  - 通知使用 ACP v1 的 update 结构
"""

import asyncio
import contextlib
import json
import os
import re
import sys
import traceback
from typing import Any

from fp_core.core.io import IOChannel
from fp_core.core.lifecycle import LifecycleHook

# ═══════════════════════════════════════════════════════════
# ACP v1 常量
# ═══════════════════════════════════════════════════════════
ACP_PROTOCOL_VERSION = 1

# ── ACP 模式下禁止暴露给 IDE 的毁灭性命令 ──
# 这些命令（exit/exit!/quit）会 raise SystemExit() 终止整个进程，
# IDE 环境中误触会导致 ACP 会话直接断开。
_DESTRUCTIVE_COMMANDS = {"exit", "exit!", "quit"}


# ═══════════════════════════════════════════════════════════
# ACPIO — ACP 专用的 IO 通道
# ═══════════════════════════════════════════════════════════


class ACPIO(IOChannel):
    """
    ACP IO 通道 — 带流式推送的缓冲输出。

    核心设计:
      - 所有 info/item/error/hint/say 调用累积到缓冲区
      - 每累积约 300 字符或显式调用 partial_flush() 时，
        通过 send_chunk 回调推送一条 agent_message_chunk
      - process() 结束后调用 flush_text() 获取剩余文本
      - 每次 ask() 先 flush 缓冲区，然后返回 "q" 取消交互

    为何需要流式？
      长时间运行的 Agent 任务（如代码生成、多步工具调用）中，
      用户需要看到渐进式反馈，而不是静默等待后突然弹出完整回复。
    """

    _STREAM_THRESHOLD = 300

    def __init__(self, send_chunk=None):
        """
        Args:
            send_chunk: 可选的回调函数，接收 (text: str)，
                        在缓冲达到阈值时自动推送 agent_message_chunk。
                        同步调用，直接写入 stdout。
        """
        self._buffer: list[str] = []
        self._send_chunk = send_chunk
        self._char_count = 0

    def _accumulate(self, text: str):
        """写入缓冲区，超过阈值时自动推送流式块"""
        self._buffer.append(str(text))
        self._char_count += len(str(text))
        if self._send_chunk and self._char_count >= self._STREAM_THRESHOLD:
            self._partial_flush()

    def _partial_flush(self):
        """推送当前缓冲区作为一条 agent_message_chunk，不清除全部"""
        if not self._buffer or not self._send_chunk:
            return
        text = "\n".join(self._buffer)
        # 标记：非最终块，由 send_chunk 决定如何处理
        self._send_chunk(text)
        self._buffer.clear()
        self._char_count = 0

    def flush_text(self) -> str:
        """返回缓冲区剩余文本并清空（process 结束后调用）"""
        if not self._buffer:
            return ""
        merged = "\n".join(self._buffer)
        self._buffer.clear()
        self._char_count = 0
        return merged

    async def ask(self, prompt: str) -> str:
        """ACP 模式下无法交互，返回 'q' 取消"""
        return "q"

    def say(self, text: str):
        self._accumulate(text)

    def info(self, text: str):
        self._accumulate(text)

    def hint(self, text: str):
        self._accumulate(text)

    def error(self, text: str):
        self._accumulate(text)

    def item(self, text: str):
        self._accumulate(text)


class ACPServer:
    """
    ACP (Agent Client Protocol) v1 服务器

    通过 stdin/stdout 与 IDE 通信，每条消息为一行 JSON（newline-delimited JSON）。

    实现的方法:
      initialize          协议握手，协商版本和能力
      session/new         创建新对话会话
      session/load        恢复已有会话
      session/prompt      发送用户消息并获取 AI 回复（核心交互）
      session/set_mode    切换 Agent 模式

    实现的通知:
      session/cancel      取消当前操作
      initialzed          客户端已就绪

    发出的通知:
      session/update      推送计划/工具调用/回复
                          Follow Agent: 文件操作时通知 IDE 打开对应文件
    """

    def __init__(self, agent_instance=None):
        """
        Args:
            agent_instance: 可选，注入已存在的 Agent 实例。
                            为 None 时自动创建新实例。
        """
        os.environ.setdefault("FP_SUBAGENT_QUIET", "1")

        # ── 保存真正的 stdout 引用（JSON-RPC 通道） ──
        self._stdout = sys.stdout

        # ── 创建 Agent（print/display 重定向到 stderr） ──
        if agent_instance is not None:
            self._agent = agent_instance
        else:
            from fp_core.core.agent import Agent

            with contextlib.redirect_stdout(sys.stderr):
                self._agent = Agent(enable_log=False)

        self._session_id: str | None = None

        # ── 工具调用追踪（配对 call ↔ result 通知） ──
        self._tool_call_counter = 0
        self._active_tool_call_ids: dict[str, str] = {}

        # ── 工具参数缓存（call → result 阶段共享） ──
        self._last_edit_args: dict | None = None  # edit_file 参数，用于构建 diff
        self._last_read_path: str | None = None  # read_file 路径，用于 result 的 resource URI
        self._last_write_path: str | None = None  # write_file 路径，用于 result 展示内容

        # ── 当前 prompt 处理 task（用于取消） ──
        self._current_task: asyncio.Task | None = None

        # ── 并发 prompt 防护 ──
        self._processing_prompt = False

        # ── "Follow Agent" 跟踪注册 ──
        self._register_follow_hooks()

    # ═══════════════════════════════════════════════════════
    # "Follow Agent" — 让 IDE 跟踪 Agent 文件操作
    # ═══════════════════════════════════════════════════════

    # bash 命令中的文件路径匹配模式
    _FILE_PATH_PATTERN = re.compile(
        r"(?:cat|less|more|head|tail|vim|nano|code|xdg-open|less|grep|rg|sed|awk)\s+"
        r'([^\s;|&`\'"()]+)'
    )

    def _register_follow_hooks(self):
        """注册生命周期钩子，在工具调用时通知 IDE 关注文件"""

        mgr = self._agent.lifecycle

        # ── 工具调用前：提取文件路径，通知 IDE 打开 ──
        mgr.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            name="acp_follow_tool_call",
            priority=1000,  # 高优先级
        )

        # ── 工具完成后：通知 IDE 工具已完成 ──
        mgr.register(
            LifecycleHook.ON_TOOL_RESULT,
            self._on_tool_result,
            name="acp_follow_tool_result",
            priority=1000,
        )

    def _extract_file_path(self, tool_name: str, args: dict) -> str | None:
        """
        从工具调用参数中提取文件路径。

        支持的工具:
          read_file/write_file/edit_file → file_path 参数
          file_fingerprint/elf_analysis  → file_path 参数
          bash                           → 从命令中猜测文件路径

        参数 args 应为已解析为 dict 的参数字典，由调用方保证。
        """
        if not isinstance(args, dict):
            return None

        # 直接有 file_path 参数的
        fp = args.get("file_path")
        if fp and isinstance(fp, str) and os.path.isfile(fp):
            return os.path.abspath(fp)

        # bash 命令中尝试提取文件路径
        if tool_name == "bash":
            cmd = args.get("command", "")
            if cmd:
                match = self._FILE_PATH_PATTERN.search(cmd)
                if match:
                    potential = os.path.abspath(os.path.expanduser(match.group(1)))
                    if os.path.isfile(potential):
                        return potential

        return None

    def _get_tool_kind(self, tool_name: str) -> str:
        """返回工具类型分类，供 IDE 显示"""
        if tool_name in ("read_file",):
            return "read"
        elif tool_name in ("write_file", "edit_file"):
            return "edit"
        elif tool_name in ("bash",):
            return "bash"
        elif tool_name in ("file_fingerprint", "elf_analysis"):
            return "analyze"
        else:
            return "other"

    async def _on_tool_call(self, context, tool_name="", tool_args="", **kwargs):
        """工具调用前 — 发送 tool_call 通知

        ACP v1 规范使用 rawInput 传递原始工具参数。
        content 用于富内容展示（如文件预览 resource）。
        """
        # ── 会话未就绪时跳过（start() 之前触发的钩子） ──
        if self._session_id is None:
            return

        # ── 解析参数 ──
        args_dict = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            args_dict = json.loads(tool_args) if isinstance(tool_args, str) else {}

        # ── 缓存 edit_file 参数供 _on_tool_result 构建 diff ──
        if tool_name == "edit_file":
            self._last_edit_args = args_dict

        # ── 缓存 read_file 路径供 _on_tool_result 构建 resource URI ──
        if tool_name == "read_file":
            fp = args_dict.get("file_path", "")
            self._last_read_path = fp if isinstance(fp, str) else None
        if tool_name == "write_file":
            fp = args_dict.get("file_path", "")
            self._last_write_path = fp if isinstance(fp, str) else None

        # ── 构建通知 ──
        kind = self._get_tool_kind(tool_name)
        file_path = self._extract_file_path(tool_name, args_dict)
        title = self._build_tool_title(tool_name, args_dict, kind)

        notification: dict = {
            "sessionId": self._session_id,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": self._tool_call_id(tool_name),
                "title": title,
                "kind": kind,
                "status": "in_progress",
                # ACP v1 规范: rawInput = 格式化字符串展示工具参数
                "rawInput": self._build_human_description(tool_name, args_dict),
            },
        }

        # 有文件路径就加上 content (resource) 供 IDE 预览
        if file_path and os.path.isfile(file_path):
            notification["update"]["content"] = [
                {
                    "type": "resource",
                    "resource": {
                        "uri": f"file://{file_path}",
                        "mimeType": self._guess_mime(file_path),
                    },
                }
            ]

        self._send_notification("session/update", notification)

    # ── 工具标题和 input 构建 ────────────────────────────

    # Emoji 分类，让 IDE 的 tool_call 面板一目了然
    _TOOL_EMOJI: dict[str, str] = {
        "read": "📖",
        "edit": "✏️",
        "bash": "🖥️",
        "analyze": "🔍",
        "other": "🔧",
    }

    def _build_tool_title(self, tool_name: str, args: dict, kind: str) -> str:
        """构建一目了然的工具标题，含 emoji 和关键参数"""
        emoji = self._TOOL_EMOJI.get(kind, "🔧")

        if tool_name == "bash":
            cmd = args.get("command", "")
            first_line = cmd.split("\n")[0] if cmd else ""
            if len(first_line) > 100:
                first_line = first_line[:100] + "…"
            return f"{emoji} {first_line}" if first_line else f"{emoji} bash"
        elif tool_name == "read_file":
            fp = args.get("file_path", "?")
            return f"{emoji} {fp}"
        elif tool_name == "write_file":
            fp = args.get("file_path", "?")
            return f"{emoji} 写入 {os.path.basename(fp)}"
        elif tool_name == "edit_file":
            fp = args.get("file_path", "?")
            return f"{emoji} 编辑 {os.path.basename(fp)}"
        elif tool_name == "python":
            code = args.get("code", "")
            first_line = code.split("\n")[0][:70] if code else ""
            return f"{emoji} 🐍 {first_line}" if first_line else f"{emoji} 🐍 python"
        elif tool_name == "web_search":
            q = args.get("query", "?")
            return f"{emoji} 搜索: {q[:60]}{'…' if len(q) > 60 else ''}"
        elif tool_name == "subagent":
            task = args.get("task", "")
            summary = task[:60] + "…" if len(task) > 60 else task
            return f"{emoji} 子任务: {summary}"
        elif tool_name == "web_fetch":
            url = args.get("url", "?")
            return f"{emoji} 抓取: {url[:60]}"
        elif tool_name == "file_fingerprint":
            fp = args.get("file_path", "?")
            return f"{emoji} 指纹: {os.path.basename(fp)}"
        elif tool_name == "elf_analysis":
            fp = args.get("file_path", "?")
            return f"{emoji} ELF: {os.path.basename(fp)}"
        return f"{emoji} {tool_name}"

    def _build_human_description(self, tool_name: str, args: dict) -> str:
        """构建人类可读的工具调用描述（放在 content 中替代 JSON 展示）"""
        if tool_name == "bash":
            return args.get("command", "")
        elif tool_name == "read_file":
            fp = args.get("file_path", "")
            off = args.get("offset")
            lim = args.get("limit")
            parts = [f"📄 {fp}"]
            if off is not None and lim is not None:
                parts.append(f"读取行 {off}~{off + lim - 1}")
            elif off is not None:
                parts.append(f"从第 {off} 行开始")
            elif lim is not None:
                parts.append(f"前 {lim} 行")
            return "\n".join(parts)
        elif tool_name == "write_file":
            fp = args.get("file_path", "")
            content = args.get("content", "")
            return f"✏️  写入 {fp}\n{content}"
        elif tool_name == "edit_file":
            fp = args.get("file_path", "")
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            return f"✏️  编辑 {fp}\n- {old}\n+ {new}"
        elif tool_name == "python":
            code = args.get("code", "")
            return f"🐍 Python:\n{code}"
        elif tool_name == "web_search":
            q = args.get("query", "")
            return f"🔍 搜索: {q}"
        elif tool_name == "web_fetch":
            url = args.get("url", "")
            return f"🌐 抓取: {url}"
        elif tool_name == "subagent":
            task = args.get("task", "")
            ctx = args.get("context", "")
            parts = [f"🔧 子任务: {task}"]
            if ctx:
                parts.append(f"  上下文: {ctx[:200]}")
            return "\n".join(parts)
        elif tool_name in ("file_fingerprint", "elf_analysis"):
            fp = args.get("file_path", "")
            return f"🔍 分析: {fp}"
        return str(args)

    @staticmethod
    def _extract_display_result(tool_name: str, result_str: str) -> str:
        """从工具结果中提取适合展示的内容。

        subagent 返回 JSON 包裹 {status, result, ...}，
        需要抽取 result 字段供用户阅读。其他工具直接返回原文。
        """
        if tool_name == "subagent" and result_str:
            with contextlib.suppress(json.JSONDecodeError, KeyError, TypeError):
                parsed = json.loads(result_str)
                if isinstance(parsed, dict) and "result" in parsed:
                    result_str = parsed["result"]
        # 强制将 \n 替换为 \n\n，防止 IDE 吞掉换行
        return result_str.replace("\n", "\n\n")

    async def _on_tool_result(self, context, tool_name="", result="", **kwargs):
        """工具完成后 — 发送 tool_call_update

        ACP v1 规范使用 rawOutput 传递原始工具结果。
        content 用于富内容渲染（resource 代码块 / write_file 内容 / diff 差异视图）。
        """
        # ── 会话未就绪时跳过 ──
        if self._session_id is None:
            return

        status = "failed" if "❌" in str(result)[:10] else "completed"
        result_str = str(result)

        # ── 清理结果：subagent 抽取 result 字段 ──
        display_str = self._extract_display_result(tool_name, result_str)

        notification: dict = {
            "sessionId": self._session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": self._tool_call_id_for_result(tool_name),
                "status": status,
                # ACP v1 规范: rawOutput = 展示用的文本（subagent 已去 JSON 包裹）
                "rawOutput": display_str if display_str else None,
            },
        }

        # ── content：富内容块 ──
        content_blocks: list[dict] = []

        # 所有工具的结果用 resource/text/plain 展示（保留换行和缩进）
        # text 类型会吞掉换行变为单行，resource 才能正确保留格式
        # read_file 跳过——它用专用 resource 代码块展示文件内容
        if display_str and tool_name not in ("read_file",):
            content_blocks.append({
                "type": "resource",
                "resource": {
                    "uri": "inline://tool-output",
                    "mimeType": "text/plain",
                    "text": display_str,
                },
            })

        # read_file 额外加 resource 代码块
        if tool_name == "read_file" and status != "failed":
            read_path = self._last_read_path or "(inline)"
            content_blocks.append({
                "type": "resource",
                "resource": {
                    "uri": f"file://{read_path}",
                    "mimeType": self._guess_mime(read_path) if read_path != "(inline)" else "text/plain",
                    "text": result_str,
                },
            })

        # write_file 额外加 resource 展示写入内容
        if tool_name == "write_file" and status != "failed" and self._last_write_path:
            write_path = self._last_write_path
            if os.path.isfile(write_path):
                with open(write_path, encoding="utf-8", errors="replace") as f:
                    file_content = f.read()
                content_blocks.append({
                    "type": "resource",
                    "resource": {
                        "uri": f"file://{write_path}",
                        "mimeType": self._guess_mime(write_path),
                        "text": file_content,
                    },
                })

        # edit_file 加 diff 块
        if tool_name == "edit_file" and status == "completed" and self._last_edit_args:
            args = self._last_edit_args
            old_text = args.get("old_string", "")
            new_text = args.get("new_string", "")
            file_path = args.get("file_path", "")
            if old_text and new_text and file_path:
                content_blocks.append({
                    "type": "diff",
                    "path": file_path,
                    "oldText": old_text,
                    "newText": new_text,
                })

        if content_blocks:
            notification["update"]["content"] = content_blocks

        self._send_notification("session/update", notification)

    def _tool_call_id(self, tool_name: str) -> str:
        """生成唯一的 toolCallId，并记录到活跃表供 result 阶段回查"""
        self._tool_call_counter += 1
        call_id = f"fp_{tool_name}_{self._tool_call_counter}"
        self._active_tool_call_ids[tool_name] = call_id
        return call_id

    def _tool_call_id_for_result(self, tool_name: str) -> str:
        """取最近一次同名 tool_call 的 ID，用于配对 tool_call_update"""
        return self._active_tool_call_ids.get(tool_name, f"fp_{tool_name}_unknown")

    @staticmethod
    def _guess_mime(file_path: str) -> str:
        """根据文件扩展名猜测 MIME 类型"""
        ext = os.path.splitext(file_path)[1].lower()
        mime_map = {
            ".py": "text/x-python",
            ".js": "text/javascript",
            ".ts": "text/typescript",
            ".jsx": "text/jsx",
            ".tsx": "text/tsx",
            ".json": "application/json",
            ".md": "text/markdown",
            ".html": "text/html",
            ".css": "text/css",
            ".c": "text/x-c",
            ".cpp": "text/x-c++",
            ".h": "text/x-c",
            ".hpp": "text/x-c++",
            ".rs": "text/rust",
            ".go": "text/x-go",
            ".java": "text/x-java",
            ".yaml": "text/yaml",
            ".yml": "text/yaml",
            ".toml": "text/toml",
            ".sh": "text/x-shellscript",
            ".txt": "text/plain",
            ".csv": "text/csv",
            ".xml": "text/xml",
            ".sql": "text/x-sql",
            ".rb": "text/x-ruby",
            ".php": "text/x-php",
        }
        return mime_map.get(ext, "text/plain")

    # ═══════════════════════════════════════════════════════
    # 公共接口
    # ═══════════════════════════════════════════════════════

    async def start(self):
        """启动 ACP 服务器，从 stdin 读取 JSON-RPC 消息"""
        await self._agent.ensure_initialized()
        self._session_id = self._agent.session.session_id

        self._log(f"✅  ACP Server 启动 (session={self._session_id})")
        self._log(f"   模型: {self._agent.model}")

        # ── 设置异步 stdin 读取器 ──
        loop = asyncio.get_event_loop()
        reader = asyncio.StreamReader()
        protocol = asyncio.StreamReaderProtocol(reader)
        await loop.connect_read_pipe(lambda: protocol, sys.stdin)

        try:
            await self._read_loop(reader)
        except asyncio.CancelledError:
            self._log("收到取消信号，正在关闭...")
        except Exception as e:
            self._log(f"意外错误: {e}")
            traceback.print_exc(file=sys.stderr)
        finally:
            await self._shutdown_agent()

    # ═══════════════════════════════════════════════════════
    # 消息循环
    # ═══════════════════════════════════════════════════════

    async def _read_loop(self, reader: asyncio.StreamReader):
        """从 stdin 逐行读取 JSON-RPC 消息（全异步派发）

        关键设计：所有请求/通知都通过 create_task 异步派发，不阻塞 stdin 读取循环。
        这样 session/cancel 通知可以在 prompt 处理期间被读取和派发，
        实现对正在运行的 agent 真正中断。
        """
        active_tasks: set[asyncio.Task] = set()

        while True:
            line = await reader.readline()
            if not line:
                self._log("stdin 已关闭 (EOF)，等待活跃任务完成...")
                if active_tasks:
                    await asyncio.gather(*active_tasks, return_exceptions=True)
                break

            text = line.decode("utf-8").strip()
            if not text:
                continue

            try:
                request: dict = json.loads(text)
            except json.JSONDecodeError as e:
                self._log(f"⚠️  JSON 解析失败: {e}")
                continue

            # 创建独立 task 派发，不阻塞 stdin 读取
            task = asyncio.create_task(self._dispatch(request))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    async def _dispatch(self, request: dict):
        """分发 JSON-RPC 请求到对应的处理器"""
        method = request.get("method", "")
        req_id = request.get("id")
        params = request.get("params", {})

        # ── 通知（无 id）的处理 ──
        if req_id is None:
            await self._handle_notification(method, params)
            return

        # ── 请求（有 id）的处理 ──
        handler = self._get_handler(method)
        if handler is None:
            self._send_error(req_id, -32601, f"Method not found: {method}")
            return

        try:
            with contextlib.redirect_stdout(sys.stderr):
                result = await handler(params)

            self._send_result(req_id, result)

            # session/new 和 session/load 之后，等待 IDE 完全消化响应并注册 sessionId，
            # 然后再发送 available_commands_update 通知。直接连续发送会导致 Zed
            # 在处理通知时 session 尚未注册，报 "unknown session" 并丢弃命令列表。
            if method in ("session/new", "session/load"):
                await asyncio.sleep(0.05)
                self._send_available_commands()
        except asyncio.CancelledError:
            self._send_result(req_id, None)
        except Exception as e:
            self._log(f"❌  处理 {method} 失败: {e}")
            traceback.print_exc(file=sys.stderr)
            self._send_error(req_id, -32603, str(e))

    def _get_handler(self, method: str):
        """根据方法名查找对应的处理器"""
        handlers = {
            "initialize": self._handle_initialize,
            "session/new": self._handle_session_new,
            "session/load": self._handle_session_load,
            "session/prompt": self._handle_session_prompt,
            "session/list": self._handle_session_list,
            "session/commands": self._handle_session_commands,
            "session/set_mode": self._handle_session_set_mode,
            "session/ping": self._handle_session_ping,
        }
        return handlers.get(method)

    async def _handle_notification(self, method: str, params: dict):
        """处理通知（无 id 的请求）"""
        if method == "session/cancel":
            self._log("⏹️  收到取消通知，正在中断 agent...")
            self._agent.cancel()
            # 主动取消当前正在执行的 prompt task，让 agent 立即停止
            # 而不是等到下一个中断检查点（可能在 LLM 调用或工具执行中，会等很久）
            if self._current_task is not None and not self._current_task.done():
                self._current_task.cancel()
                self._log("  → 已取消当前处理 task")
        elif method == "initialized":
            self._log("客户端已初始化")
            # 不在此处注册命令——此时只有默认 sessionId（IDE 不认识它），
            # 命令由 session/new 和 session/load handler 在正确的会话中注册。
        else:
            pass

    # ═══════════════════════════════════════════════════════
    # ACP v1 方法处理器
    # ═══════════════════════════════════════════════════════

    async def _handle_initialize(self, params: dict) -> dict:
        """
        ACP v1 协议握手。

        参考: https://agentclientprotocol.com/protocol/v1/initialization
        """
        client_info = params.get("clientInfo", {})
        self._log(f"客户端连接: {client_info.get('name', '?')} v{client_info.get('version', '?')}")

        return {
            "protocolVersion": ACP_PROTOCOL_VERSION,
            "agentCapabilities": {
                "loadSession": True,
                "sessionCapabilities": {
                    "list": {},
                },
                "promptCapabilities": {
                    "image": False,
                    "audio": False,
                    "embeddedContext": True,
                },
                "mcpCapabilities": {
                    "http": False,
                    "sse": False,
                },
            },
            "agentInfo": {
                "name": "five-pebbles",
                "title": "Five Pebbles",
                "version": "0.1.0",
            },
            "authMethods": [],
        }

    async def _handle_session_new(self, params: dict) -> dict:
        """
        ACP v1 创建新会话。

        创建后立即通过 notification 注册所有斜杠命令，
        否则 Zed 会拦截所有未注册的 / 命令。

        参考: https://agentclientprotocol.com/protocol/v1/session-setup
        """
        self._agent.save_context()
        new_sid = self._agent.session.create_session()
        self._agent.rebuild_context()
        self._session_id = new_sid
        self._log(f"创建新会话: {new_sid}")

        # 注意：命令注册通知在 _dispatch 中响应之后发送，
        # 以确保 IDE 先拿到 sessionId 再处理命令列表。
        return {"sessionId": new_sid}

    async def _handle_session_load(self, params: dict) -> dict:
        """
        ACP v1 恢复已有会话。

        如果会话不存在，自动创建新的空会话返回（而不是报错让 IDE 崩溃）。

        参考: https://agentclientprotocol.com/protocol/v1/session-setup#loading-sessions
        """
        # ACP v1 使用 camelCase
        session_id = params.get("sessionId") or params.get("session_id", "")
        if not session_id:
            return {"sessionId": self._agent.session.session_id}

        self._agent.save_context()
        if self._agent.switch_session(session_id):
            self._session_id = session_id
            self._log(f"恢复会话: {session_id}")

            # 注意：命令注册通知在 _dispatch 中响应之后发送
            return {"sessionId": session_id}
        else:
            self._log(f"会话不存在: {session_id}，自动创建新会话")
            new_sid = self._agent.session.create_session()
            self._agent.rebuild_context()
            self._session_id = new_sid
            return {"sessionId": new_sid}

    async def _handle_session_list(self, params: dict) -> dict:
        """
        ACP v1 列出所有历史会话。

        参考: https://agentclientprotocol.com/protocol/v1/session-list
        """
        all_sessions = self._agent.session.list_sessions()
        current_sid = self._agent.session.session_id

        sessions_list = []
        for sid, meta in sorted(
            all_sessions.items(),
            key=lambda x: x[1].get("updated", x[1].get("created", "")),
            reverse=True,
        ):
            title = meta.get("summary", "") or ""
            msg_count = meta.get("message_count", 0)

            session_entry = {
                "sessionId": sid,
                "title": title[:80] if title else None,
                "updatedAt": meta.get("updated", meta.get("created", "")),
                "_meta": {
                    "messageCount": msg_count,
                    "isCurrent": sid == current_sid,
                },
            }
            sessions_list.append(session_entry)

        return {"sessions": sessions_list}

    async def _handle_session_prompt(self, params: dict) -> dict:
        """
        ACP v1 核心交互：发送用户消息并返回 AI 回复。

        ACP v1 规范要求：
          - 回复内容通过 session/update (agent_message_chunk) 通知发送
          - 响应 result 只包含 stopReason

        支持两种请求格式：
          1. ACP v1 标准: prompt 数组 (content blocks)
          2. 兼容格式:     messages 数组 (OpenAI-style)

        参考: https://agentclientprotocol.com/protocol/v1/prompt-turn
        """
        # ── 提取用户消息 ──
        user_prompt = self._extract_prompt(params)

        if user_prompt is None:
            raise ValueError("No prompt provided. Use 'prompt' (ACP format) or 'messages' (OpenAI format)")

        # ── 并发防护：同一时间只能处理一个 prompt ──
        if self._processing_prompt:
            self._log("⚠️  并发 prompt 请求被拒绝")
            return {"stopReason": "cancelled"}
        self._processing_prompt = True

        # ── 快照当前 sessionId，防止在异步处理期间被 session/new 修改 ──
        sid = self._session_id

        try:
            # ── 发送 plan 通知 ──
            self._send_plan_notification(session_id=sid)

            # ── 调用 Agent 核心 ──
            # 使用 ACPIO 缓冲输出，并在累积到阈值时自动推送 agent_message_chunk
            #
            # 注意：send_chunk 回调捕获了 sid 快照，避免 race
            acp_io = ACPIO(send_chunk=lambda text: self._send_message_notification(text, session_id=sid))

            self._log(f"发送给 Agent: {user_prompt[:120]}")
            self._current_task = asyncio.create_task(self._agent.process(user_prompt, io=acp_io))
            try:
                response = await self._current_task
            except asyncio.CancelledError:
                self._log("⏹️  Agent 处理已被用户取消")
                # 清理中断标记，确保下次 prompt 正常工作
                self._agent._interrupted = False
                self._agent._processing = False
                # 取消后仍然 flush 缓冲区，确保用户看到已生成的内容
                buf = acp_io.flush_text()
                if buf.strip():
                    self._send_message_notification(buf, session_id=sid)
                return {"stopReason": "cancelled"}
            finally:
                self._current_task = None

            # ── 处理剩余文本（流式推送后缓冲区可能已空） ──
            buf = acp_io.flush_text()
            reply_text = response.content or ""

            if buf.strip():
                self._send_message_notification(buf, session_id=sid)

            if reply_text and reply_text not in buf:
                self._send_message_notification(reply_text, session_id=sid)

            return {"stopReason": "end_turn"}
        finally:
            self._processing_prompt = False

    async def _handle_session_set_mode(self, params: dict) -> dict:
        """切换 Agent 模式（预留）"""
        mode = params.get("mode", "normal")
        return {"mode": mode}

    async def _handle_session_ping(self, params: dict) -> dict:
        """ACP v1 心跳保持"""
        return {"pong": True}

    async def _handle_session_commands(self, params: dict) -> dict:
        """
        返回可用斜杠命令列表（作为 session/commands 请求的 fallback）。

        标准 ACP v1 通过 available_commands_update 通知注册命令，
        此 handler 作为补充，覆盖 IDE 通过请求方式查询命令的场景。

        命令名不带 '/' 前缀（ACP v1 约定），IDE 端会自行添加。
        """
        try:
            from fp_core.commands import get_all_commands

            cmds = get_all_commands()
            # 过滤掉毁灭性命令（exit/exit!/quit 等会 raise SystemExit 的命令）
            cmds = {k: v for k, v in cmds.items() if k not in _DESTRUCTIVE_COMMANDS}
            # ACP v1 协议中命令名不带 '/' 前缀
            commands = [{"name": name, "description": desc} for name, desc in sorted(cmds.items())]
            return {"commands": commands}
        except Exception as e:
            self._log(f"获取命令列表失败: {e}")
            return {"commands": []}

    # ═══════════════════════════════════════════════════════
    # 辅助方法
    # ═══════════════════════════════════════════════════════

    def _extract_prompt(self, params: dict) -> str | None:
        """
        从 ACP v1 请求中提取用户消息文本。

        优先级:
          1. params.prompt (ACP v1 标准 content blocks)
          2. params.messages (OpenAI 兼容格式)

        ACP v1 标准 prompt 示例:
          [
            {"type": "text", "text": "Hello"},
            {"type": "resource", "resource": {"uri": "file:///...", "text": "..."}}
          ]

        OpenAI 兼容 messages 示例:
          [
            {"role": "user", "content": "Hello"}
          ]
        """
        # ── 方式1: ACP v1 标准 prompt ──
        prompt_list = params.get("prompt")
        if prompt_list is not None and isinstance(prompt_list, list) and len(prompt_list) > 0:
            return self._prompt_blocks_to_text(prompt_list)

        # ── 方式2: OpenAI 兼容 messages ──
        messages = params.get("messages")
        if messages is not None and isinstance(messages, list) and len(messages) > 0:
            last_msg = messages[-1]
            if last_msg.get("role") in ("user", "assistant"):
                return self._build_messages_text(messages)

        return None

    def _prompt_blocks_to_text(self, blocks: list[dict]) -> str:
        """
        将 ACP v1 的 content blocks 转换为文本。

        支持:
          - text block: {"type": "text", "text": "..."}
          - resource block: {"type": "resource", "resource": {"uri": "...", "text": "...", ...}}

        组合规则：
          - 所有 text blocks 按顺序拼接
          - resource blocks 转换为 [文件: uri]\n内容 格式
        """
        parts = []
        for block in blocks:
            block_type = block.get("type", "")

            if block_type == "text":
                text = block.get("text", "")
                if text:
                    parts.append(text)

            elif block_type == "resource":
                resource = block.get("resource", {})
                uri = resource.get("uri", "")
                text = resource.get("text", "") or resource.get("content", "")
                mime = resource.get("mimeType", "")

                header = f"[文件: {uri}]"
                if mime:
                    header += f" ({mime})"
                parts.append(f"{header}\n{text}")

        return "\n".join(parts)

    def _build_messages_text(self, messages: list[dict]) -> str:
        """
        从 OpenAI 格式的 messages 中提取最后一条 user 消息。

        Agent 自己管理对话历史（self._conv.messages），
        不需要外部拼接历史轮次。只提取最新用户输入。
        """
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                return content if isinstance(content, str) else str(content)
        return ""

    def _send_available_commands(self):
        """注册所有斜杠命令到 Zed（通过 available_commands_update 通知）

        Zed 的聊天面板维护一个白名单，只有注册过的 / 命令才会发给 Agent，
        未注册的直接在 UI 报错"is not a recognized command"。

        命令名不含 '/' 前缀（ACP v1 规范），IDE 端在匹配用户输入时会自行添加。
        官方规范参考: https://agentclientprotocol.com/protocol/slash-commands

        调用时机：由 _dispatch 在 session/new 和 session/load 的响应之后发送，
        确保 IDE 先拿到 sessionId 再处理命令列表。
        """
        try:
            from fp_core.commands import get_all_commands

            cmds = get_all_commands()
            # 过滤掉毁灭性命令（exit/exit!/quit 等会 raise SystemExit 的命令）
            cmds = {k: v for k, v in cmds.items() if k not in _DESTRUCTIVE_COMMANDS}
            # ACP v1 规范: name 字段不包含 '/' 前缀
            # 示例: {"name": "help", "description": "显示此帮助"}
            available = [{"name": name, "description": desc} for name, desc in sorted(cmds.items())]

            if available:
                self._send_notification(
                    "session/update",
                    {
                        "sessionId": self._session_id,
                        "update": {
                            "sessionUpdate": "available_commands_update",
                            "availableCommands": available,
                        },
                    },
                )
                self._log(f"已注册 {len(available)} 个斜杠命令")
        except Exception as e:
            self._log(f"注册斜杠命令失败: {e}")

    def _send_plan_notification(self, session_id=None):
        """发送 ACP v1 plan 更新通知"""
        sid = session_id or self._session_id
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "plan",
                    "entries": [
                        {
                            "content": "五块卵石正在思考...",
                            "priority": "high",
                            "status": "running",
                        },
                    ],
                },
            },
        )

    def _send_message_notification(self, text: str, session_id=None):
        """通过 agent_message_chunk 通知发送回复内容"""
        import uuid

        sid = session_id or self._session_id
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": f"msg_{uuid.uuid4().hex[:12]}",
                    "content": {
                        "type": "text",
                        "text": text,
                    },
                },
            },
        )

    async def _shutdown_agent(self):
        """安全关闭 Agent"""
        try:
            self._agent.set_nuclear_exit()
            with contextlib.redirect_stdout(sys.stderr):
                await self._agent.shutdown()
        except Exception as e:
            self._log(f"关闭 Agent 时出错: {e}")

    # ═══════════════════════════════════════════════════════
    # JSON-RPC 通信（写真正的 stdout）
    # ═══════════════════════════════════════════════════════

    def _send_result(self, req_id: Any, result: dict | None):
        """发送 JSON-RPC 成功响应"""
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "result": result},
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()

    def _send_error(self, req_id: Any, code: int, message: str):
        """发送 JSON-RPC 错误响应"""
        msg = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": req_id,
                "error": {"code": code, "message": message},
            },
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()

    def _send_notification(self, method: str, params: dict):
        """发送 JSON-RPC 通知（无 id）"""
        msg = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params},
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()

    @staticmethod
    def _log(msg: str):
        """日志输出到 stderr"""
        print(f"[ACP] {msg}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════


def main():
    """启动 ACP Server 的入口函数"""
    server = ACPServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("[ACP] 用户中断", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
