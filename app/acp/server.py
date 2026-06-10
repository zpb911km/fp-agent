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
import time
import traceback
from typing import Any

from core.lifecycle import LifecycleHook

# ── 路径修复 ─────────────────────────────────────────────
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ═══════════════════════════════════════════════════════════
# ACP v1 常量
# ═══════════════════════════════════════════════════════════
ACP_PROTOCOL_VERSION = 1


# ═══════════════════════════════════════════════════════════
# ACPIO — ACP 专用的 IO 通道
# ═══════════════════════════════════════════════════════════


class ACPIO:
    """
    ACP IO 通道 — 缓冲所有输出后一次性发送。

    核心设计:
      - 所有 info/item/error/hint/say 调用都写入缓冲区
      - 每次 ask() 先 flush 缓冲区，然后返回 "q" 取消交互
      - handler 在 process() 完成后调用 flush() 发送最终合并的消息

    为何缓冲？
      命令如 /help、/back、/resume 会调用多次 io.info() / io.item()，
      每条发一条 agent_message_chunk 会导致 Zed 将它们显示为多条独立消息。
      缓冲后合并为一条消息，格式正确、视觉统一。
    """

    def __init__(self):
        self._buffer: list[str] = []

    def flush_text(self) -> str:
        """返回缓冲区合并后的文本并清空缓冲区"""
        if not self._buffer:
            return ""
        merged = "\n".join(self._buffer)
        self._buffer.clear()
        return merged

    async def ask(self, prompt: str) -> str:
        """ACP 模式下无法交互，返回 'q' 取消"""
        return "q"

    def say(self, text: str):
        self._buffer.append(str(text))

    def info(self, text: str):
        self._buffer.append(str(text))

    def hint(self, text: str):
        self._buffer.append(str(text))

    def error(self, text: str):
        self._buffer.append(str(text))

    def item(self, text: str):
        self._buffer.append(str(text))


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
            from core.agent import Agent

            with contextlib.redirect_stdout(sys.stderr):
                self._agent = Agent(enable_log=False)

        self._session_id: str | None = None

        # ── 编辑上下文缓存（用于生成 diff） ──
        self._last_edit_args: dict | None = None

        # ── "Follow Agent" 跟踪注册 ──
        self._register_follow_hooks()

    # ═══════════════════════════════════════════════════════
    # "Follow Agent" — 让 IDE 跟踪 Agent 文件操作
    # ═══════════════════════════════════════════════════════

    # 定义哪些工具涉及文件操作
    _FILE_TOOLS = frozenset({
        "read_file",
        "write_file",
        "edit_file",
        "file_fingerprint",
        "elf_analysis",
    })

    # bash 命令中的文件路径匹配模式
    _FILE_PATH_PATTERN = re.compile(
        r"(?:cat|less|more|head|tail|vim|nano|code|xdg-open|less|grep|rg|sed|awk)\s+"
        r'([^\s;|&`\'"()]+)'
    )

    def _register_follow_hooks(self):
        """注册生命周期钩子，在工具调用时通知 IDE 关注文件"""
        from core.lifecycle import LifecycleHook

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

    def _extract_file_path(self, tool_name: str, args_str: str) -> str | None:
        """
        从工具调用参数中提取文件路径。

        支持的工具:
          read_file/write_file/edit_file → file_path 参数
          file_fingerprint/elf_analysis  → file_path 参数
          bash                           → 从命令中猜测文件路径
        """
        try:
            args = json.loads(args_str) if isinstance(args_str, str) else args_str
        except json.JSONDecodeError:
            return None

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
        """工具调用前 — 发送带 input 详情的 tool_call 通知

        ACP 规范支持 input 字段显示工具原始参数。
        bash 命令显示完整命令，edit_file 显示 old→new 摘要，read_file 显示文件路径。
        """
        # ── 解析参数 ──
        args_dict = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            args_dict = json.loads(tool_args) if isinstance(tool_args, str) else {}

        # ── 构建通知 ──
        kind = self._get_tool_kind(tool_name)
        file_path = self._extract_file_path(tool_name, tool_args)
        title = self._build_tool_title(tool_name, args_dict, kind)
        input_text = self._build_tool_input(tool_name, args_dict)

        notification: dict = {
            "sessionId": self._session_id,
            "update": {
                "sessionUpdate": "tool_call",
                "toolCallId": self._tool_call_id(tool_name),
                "title": title,
                "kind": kind,
                "status": "in_progress",
            },
        }

        # 有 input 就加上
        if input_text:
            notification["update"]["input"] = {"type": "text", "text": input_text}

        # 有文件路径就加上 content (resource)
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

    def _build_tool_title(self, tool_name: str, args: dict, kind: str) -> str:
        """构建人类可读的工具标题，Zed 在 tool_call 面板展示"""
        if tool_name == "bash":
            cmd = args.get("command", "")
            return cmd.split("\n")[0][:80] if cmd else "bash"
        elif tool_name == "read_file":
            return f"read: {os.path.basename(args.get('file_path', '?'))}"
        elif tool_name == "write_file":
            return f"write: {os.path.basename(args.get('file_path', '?'))}"
        elif tool_name == "edit_file":
            return f"edit: {os.path.basename(args.get('file_path', '?'))}"
        elif tool_name == "python":
            code = args.get("code", "")
            return code.split("\n")[0][:60] if code else "python"
        elif tool_name == "web_search":
            return f"search: {args.get('query', '?')}"
        elif tool_name == "subagent":
            task = args.get("task", "")
            return task[:60] + "..." if len(task) > 60 else task
        return tool_name

    def _build_tool_input(self, tool_name: str, args: dict) -> str | None:
        """构建工具 input 文本（显示在 Zed 的 tool_call 详情中）"""
        if tool_name == "bash":
            return args.get("command", "")
        elif tool_name == "read_file":
            fp = args.get("file_path", "")
            off = args.get("offset")
            lim = args.get("limit")
            parts = [fp]
            if off is not None:
                parts.append(f"offset={off}")
            if lim is not None:
                parts.append(f"limit={lim}")
            return " ".join(parts)
        elif tool_name == "edit_file":
            old = args.get("old_string", "")
            new = args.get("new_string", "")
            old_short = old[:60] + "..." if len(old) > 60 else old
            new_short = new[:60] + "..." if len(new) > 60 else new
            return f"{args.get('file_path', '?')}\n- {old_short}\n+ {new_short}"
        elif tool_name == "write_file":
            return args.get("file_path", "")
        elif tool_name == "python":
            return args.get("code", "")[:200]
        elif tool_name == "web_search":
            return args.get("query", "")
        elif tool_name == "subagent":
            task = args.get("task", "")
            ctx = args.get("context", "")
            if ctx:
                return f"{task}\n[context] {ctx[:100]}"
            return task
        return None

    async def _on_tool_result(self, context, tool_name="", result="", **kwargs):
        """工具完成后 — 发送 tool_call_update，包含 output 和可能的 diff"""
        status = "failed" if "❌" in str(result)[:10] else "completed"
        result_str = str(result)

        notification: dict = {
            "sessionId": self._session_id,
            "update": {
                "sessionUpdate": "tool_call_update",
                "toolCallId": self._tool_call_id(tool_name),
                "status": status,
            },
        }

        # ── output：bash 显示命令输出摘要 ──
        if tool_name == "bash" and result_str:
            notification["update"]["output"] = {"type": "text", "text": result_str[:500]}

        # ── diff：edit_file 构建 diff 内容块 ──
        if tool_name == "edit_file" and status == "completed" and self._last_edit_args:
            args = self._last_edit_args
            old_text = args.get("old_string", "")
            new_text = args.get("new_string", "")
            file_path = args.get("file_path", "")
            if old_text and new_text and file_path:
                notification["update"]["content"] = [
                    {
                        "type": "diff",
                        "path": file_path,
                        "oldText": old_text,
                        "newText": new_text,
                    }
                ]

        # ── 错误时显示错误信息 ──
        if status == "failed":
            notification["update"]["output"] = {"type": "text", "text": result_str[:300]}

        self._send_notification("session/update", notification)

    @staticmethod
    def _tool_call_id(tool_name: str) -> str:
        """生成唯一的 toolCallId"""
        return f"fp_{tool_name}_{int(time.time())}"

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
        """从 stdin 逐行读取 JSON-RPC 消息"""
        while True:
            line = await reader.readline()
            if not line:
                self._log("stdin 已关闭 (EOF)，优雅退出")
                break

            text = line.decode("utf-8").strip()
            if not text:
                continue

            try:
                request: dict = json.loads(text)
            except json.JSONDecodeError as e:
                self._log(f"⚠️  JSON 解析失败: {e}")
                continue

            await self._dispatch(request)

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
            "session/set_mode": self._handle_session_set_mode,
        }
        return handlers.get(method)

    async def _handle_notification(self, method: str, params: dict):
        """处理通知（无 id 的请求）"""
        if method == "session/cancel":
            self._log("收到取消通知")
            self._agent.cancel()
        elif method == "initialized":
            self._log("客户端已初始化")
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
                "version": "2.0.0",
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

        # ── 注册所有斜杠命令（让 Zed 不拦截它们） ──
        self._send_available_commands()

        return {"sessionId": new_sid}

    async def _handle_session_load(self, params: dict) -> dict:
        """
        ACP v1 恢复已有会话。

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

            # ── 注册斜杠命令 ──
            self._send_available_commands()

            return {"sessionId": session_id}
        else:
            self._log(f"会话不存在: {session_id}")
            raise ValueError(f"Session not found: {session_id}")

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

        # ── 发送 plan 通知 ──
        self._send_plan_notification()

        # ── 注册工具调用钩子（实时通知 Zed 工具执行状态） ──
        # 生命周期钩子的签名是 func(context, **kwargs)，与 _on_tool_call 匹配
        self._agent.lifecycle.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            priority=50,
            name="acp_tool_call",
        )
        self._agent.lifecycle.register(
            LifecycleHook.ON_TOOL_RESULT,
            self._on_tool_result,
            priority=50,
            name="acp_tool_result",
        )

        # ── 调用 Agent 核心 ──
        # 使用 ACPIO 缓冲所有命令输出（info/item/error/hint），
        # 避免每条发一条 agent_message_chunk 导致 Zed 显示多条消息。
        acp_io = ACPIO()

        self._log(f"发送给 Agent: {user_prompt[:120]}")
        response = await self._agent.process(user_prompt, io=acp_io)

        # ── 注销工具调用钩子 ──
        self._agent.lifecycle.unregister(LifecycleHook.ON_TOOL_CALL, "acp_tool_call")
        self._agent.lifecycle.unregister(LifecycleHook.ON_TOOL_RESULT, "acp_tool_result")

        # ── 合并缓冲区 + response.content，一次性发送 ──
        # 命令（如 /help）将输出写入 ACPIO 缓冲区的同时还返回内容到 response.content，
        # 两者可能重复。前端优先展示缓冲区内容，跳过冗余的 response.content。
        buf = acp_io.flush_text()
        reply_text = response.content or ""

        # 合并并去重（如果缓存区已包含 reply_text，就不重复发）
        final_text = buf
        if reply_text and reply_text not in buf:
            if final_text:
                final_text += "\n"
            final_text += reply_text

        if final_text.strip():
            # 美化：检测常见模式，应用更好的 Markdown 格式
            formatted = self._format_for_acp(final_text)
            self._send_message_notification(formatted)

        # ── ACP v1 响应只包含 stopReason ──
        return {
            "stopReason": "end_turn",
        }

    async def _handle_session_set_mode(self, params: dict) -> dict:
        """切换 Agent 模式（预留）"""
        mode = params.get("mode", "normal")
        return {"mode": mode}

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
        将 OpenAI 格式的 messages 转换为单个文本字符串。

        只取最后一条消息的内容，但会附带角色前缀以提供上下文。
        """
        # 简单情况：只有一条消息
        if len(messages) == 1:
            return messages[0].get("content", "")

        # 多条消息：拼接最近的对话轮次（最多保留3轮）
        recent = messages[-4:]  # 保留最近 4 条
        parts = []
        for msg in recent:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            if content:
                parts.append(f"[{role}]\n{content}")

        return "\n\n".join(parts)

    def _send_available_commands(self):
        """注册所有斜杠命令到 Zed（通过 available_commands_update 通知）

        Zed 的聊天面板维护一个白名单，只有注册过的 / 命令才会发给 Agent，
        未注册的直接在 UI 报错"is not a recognized command"。

        参考: https://agentclientprotocol.com/protocol/v1/slash-commands
        """
        try:
            from commands import get_all_commands

            cmds = get_all_commands()
            available = []
            for name, desc in sorted(cmds.items()):
                available.append({
                    "name": name,
                    "description": desc,
                })

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

    def _send_plan_notification(self):
        """发送 ACP v1 plan 更新通知"""
        self._send_notification(
            "session/update",
            {
                "sessionId": self._session_id,
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

    @staticmethod
    def _format_for_acp(text: str) -> str:
        """美化 ACP 输出文本的 Markdown 格式

        规则：
        - 命令列表（多行，每行以 "  /" 开头）→ 代码块，保留单换行
        - 其他文本 → \n 替换为 \n\n（Markdown 段落）
        """
        lines = text.split("\n")

        # ── 检测：命令列表（至少 2 行以 "  /" 开头）──
        cmd_lines = [line for line in lines if line.strip().startswith("/") and len(line.strip()) > 3]
        if len(cmd_lines) >= 2:
            # 是否大部分行都是命令行
            non_empty = [line for line in lines if line.strip()]
            if len(cmd_lines) / max(len(non_empty), 1) > 0.3:
                formatted = "**可用命令:**\n\n```\n"
                for line in lines:
                    stripped = line.strip()
                    if (
                        stripped.startswith("/")
                        or stripped == "可用命令:"
                        or stripped
                        and not stripped.startswith("```")
                    ):
                        formatted += line.strip() + "\n"
                formatted += "```"
                return formatted

        # ── 序号列表（多行以 "  [N]" 或 "[N]" 开头）→ 保持原样 ──
        indexed = [line for line in lines if line.strip().startswith("[") and "]" in line[:10]]
        if len(indexed) >= 3:
            # 保留原格式，但用单个 \n 连接（紧凑），段落用 \n\n
            return text

        # ── 默认：\n → \n\n（Markdown 段落分隔）──
        return text.replace("\n", "\n\n")

    def _send_message_notification(self, text: str):
        """通过 agent_message_chunk 通知发送回复内容"""
        import uuid

        self._send_notification(
            "session/update",
            {
                "sessionId": self._session_id,
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
