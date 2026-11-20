"""
ACP Server — Agent Client Protocol 实现 (v1 兼容)
==================================================

将 FP 作为 ACP Server 接入 IDE（Zed / VS Code / JetBrains 等）。

协议规范：https://agentclientprotocol.com/protocol/v1
参考实现：https://github.com/agentclientprotocol/agent-client-protocol

架构：
  IDE (ACP Client)
      │  stdio (JSON-RPC 2.0, 一行一条 JSON)
      ▼
  ACP Server（本文件）
      │  redirect_stdout → stderr（日志/display）
      ▼
  Agent.process()   ← FP 核心

关键修正 (v1)：
  - protocolVersion: 1 (integer, 不是字符串)
  - 字段名使用 camelCase: agentCapabilities, agentInfo, sessionId
  - session/prompt 使用 prompt 数组 + 向后兼容 messages
  - 通知使用 ACP v1 的 update 结构
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import os
import re
import sys
import traceback
import uuid
from collections.abc import Awaitable, Callable
from contextvars import Token
from dataclasses import dataclass, field
from datetime import datetime
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as _pkg_version
from typing import Any, cast

from fp_core.api import ExitReason, HookContext, IOChannel, LifecycleHook, portal
from fp_core.api.cwd import current_cwd, reset_cwd, set_cwd
from fp_core.api.spawn import QUIET_ENV

# ═══════════════════════════════════════════════════════════
# ACP v1 常量
# ═══════════════════════════════════════════════════════════
ACP_PROTOCOL_VERSION = 1
# 本端支持的协议版本（v2 仍为 Draft，客户端请求 v2 时回退到 v1）
_SUPPORTED_PROTOCOL_VERSIONS = (1,)

# 承载 MCP 的插件名（private 插件，未加载时 mcpServers 转发降级为记日志）
_MCP_PLUGIN_NAME = "mcp"

# 本连接上最多保留多少条会话登记（超出淘汰最久未用者，防长连接泄漏）
_MAX_SESSIONS = 64

# JSON-RPC 错误码（ACP v1 ErrorCode）
_RPC_METHOD_NOT_FOUND = -32601
_RPC_INVALID_PARAMS = -32602
_RPC_INVALID_REQUEST = -32600
_RPC_INTERNAL_ERROR = -32603


class _ACPRPCError(Exception):
    """带 JSON-RPC 错误码的协议层异常（由 _dispatch 统一转为 error 响应）。"""

    def __init__(self, code: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def _agent_version() -> str:
    """agentInfo.version：取包元数据（避免与 pyproject 版本漂移）。"""
    try:
        return _pkg_version("fp-acp")
    except PackageNotFoundError:  # 源码树直跑（未安装）时的兜底
        return "0.0.0+source"


# ── session/list 的分页与时间格式（形状以 ACP v1 schema 为准）──
# 请求 zListSessionsRequest: {_meta?, additionalDirectories?, cursor?, cwd?}
#   —— **全部可选**；客户端在拿不到工作目录时直接发 `{}`
#   （如 formulahendry.acp-client：`const params = {}; if (opts.cwd) params.cwd = ...`）。
# 响应 zListSessionsResponse: {_meta?, nextCursor?, sessions: SessionInfo[]}
# SessionInfo: {_meta?, additionalDirectories?, cwd: string(必填), sessionId, title?, updatedAt?}
_LIST_PAGE_SIZE = 50

# 定位 edit_file 落点时最多读的字节数。1 MiB 足以覆盖绝大多数源码文件；
# 超大文件直接放弃定位（只发 path，不发 line）——follow-along 是提示性信息，
# 不值得为它把整个大文件读进内存。
_LOCATE_MAX_BYTES = 1 << 20


def _norm_cwd(path: str) -> str:
    """会话根归一：realpath 消软链/`..`/尾斜杠，用于 cwd 过滤比对。

    客户端传的 cwd 与落盘的 cwd 可能一个走软链、一个走实体路径，
    直接字符串比较会把同一目录判成两个（如 /media/x → /dev/sdb1）。
    """
    try:
        return os.path.realpath(os.path.abspath(path))
    except (OSError, ValueError):
        return os.path.abspath(path)


def _to_iso8601(ts: Any) -> str | None:
    """会话 meta 的本地时间串 → ISO 8601（ACP 约定 updatedAt 为 ISO 时间）。

    meta 存的是 `%Y-%m-%d %H:%M:%S`（naive 本地时间）。`astimezone()` 为
    naive 时间补本地时区偏移 → `2026-11-19T21:12:11+08:00`。已是 ISO 的
    （或格式未知的）原样返回，保证幂等。
    """
    if not isinstance(ts, str) or not ts:
        return None
    try:
        return datetime.strptime(ts, "%Y-%m-%d %H:%M:%S").astimezone().isoformat()
    except ValueError:
        return ts


# ── configOptions（Zed 里选模型的唯一通道）──
# category 枚举（schema，snake_case）：mode / model / model_config / thought_level。
# 本端只报 model：fp 没有 mode 概念（章程明确「无危险指令审批门」），
# 也没有 reasoning_effort / thinking_budget 这类可调面 —— 假的选项比没有更糟，
# 客户端会照单渲染成可点的下拉项。
_CONFIG_MODEL_ID = "model"
# 切换走**命令层**（public/commands/model.py，用户的扩展）：模型切换的全部语义
# （就地换 client、同步 agent.client、写 ACTIVE_LLM、同步 config 模块内存态）
# 只存在于那一处。ACP 若自己再实现一遍，等于两份会漂移的真相。
_MODEL_COMMAND = "model"


def _normalize_mcp_servers(raw: Any) -> tuple[dict[str, dict[str, Any]], list[str]]:
    """ACP 线格式的 `mcpServers` → 插件要的 `{name: {command, args, env}}`。

    线格式（schema v1 `McpServer`）：**数组**，元素 ``#[serde(tag = "type")]``，
    其中 stdio 变体是 untagged（无 ``type`` 键、靠 ``command`` 认领）：
      - stdio: ``{name, command, args?: [str], env?: [{name, value}]}``
      - http/sse: ``{type: "http"|"sse", name, url, headers?}``
    注意 ``env`` 是**对象数组**，不是 dict。

    旧实现按 dict 解析（``isinstance(raw, dict)``），真实客户端发数组 →
    整包被静默丢弃，连日志都没有。静默是本项目反复踩过的失败模式，
    所以这里两种形态都收：数组按 schema，dict 视为旧形态直通（向后兼容）。

    Returns:
        (servers, rejected)：rejected 是给人看的拒绝说明（不做静默忽略）。
    """
    if isinstance(raw, dict) and raw:
        # 旧形态 {name: {command, ...}}：整包直通，交给插件逐条校验
        return cast("dict[str, dict[str, Any]]", dict(cast("dict[str, Any]", raw))), []
    if not isinstance(raw, list):
        return {}, []

    servers: dict[str, dict[str, Any]] = {}
    rejected: list[str] = []
    for item in cast("list[Any]", raw):
        if not isinstance(item, dict):
            rejected.append(f"{item!r}: 不是对象")
            continue
        entry = cast("dict[str, Any]", item)
        name = entry.get("name")
        if not isinstance(name, str) or not name:
            rejected.append(f"{entry.get('type', '?')}: 缺少 name")
            continue
        cmd = entry.get("command")
        if not isinstance(cmd, str) or not cmd:
            kind = entry.get("type")
            rejected.append(f"{name}: 仅支持 stdio 传输（type={kind or '未知'}，无 command）")
            continue
        env = entry.get("env")
        env_map: dict[str, str] | None = None
        if isinstance(env, list):
            # schema：env 是 [{name, value}] 对象数组
            env_map = {}
            for pair in cast("list[Any]", env):
                if not isinstance(pair, dict):
                    continue
                # 显式窄化到 dict[str, Any]：直接 pair.get 会让 pyright 把 pair 当
                # Unknown，报 reportUnknownMemberType（strict 下是错，不是警告）。
                pair_map = cast("dict[str, Any]", pair)
                pair_name = pair_map.get("name")
                if isinstance(pair_name, str):
                    env_map[pair_name] = str(pair_map.get("value") or "")
        elif isinstance(env, dict):
            env_map = {str(k): str(v) for k, v in cast("dict[str, Any]", env).items()}
        cfg: dict[str, Any] = {"command": cmd}
        args = entry.get("args")
        if isinstance(args, list):
            cfg["args"] = [str(a) for a in cast("list[Any]", args)]
        if env_map:
            cfg["env"] = env_map
        servers[name] = cfg
    return servers, rejected


def _llm_config() -> Any:
    """懒取 fp_core.config（延迟 import：本模块导入期不读用户配置）。"""
    from fp_core import config as _cfg

    return _cfg


def _model_catalog() -> list[tuple[str, str]]:
    """用户 `LLM_PROVIDERS` 表展平为有序 [(provider, model)]（只读，不写盘）。

    顺序 = 配置表内的顺序，与 `/model` 列表序号一致（所见即所用）。
    无表 / 配置损坏 → []（调用方据此**不报** configOptions，而不是给空选项）。
    """
    cfg_mod = _llm_config()
    try:
        with open(cfg_mod.USER_CONFIG_PATH, encoding="utf-8") as f:
            raw: Any = json.load(f)
    except (OSError, ValueError, TypeError):
        return []
    if not isinstance(raw, dict):
        return []
    providers = cfg_mod.normalize_providers(cast("dict[str, Any]", raw).get("LLM_PROVIDERS"))
    return [(p, m) for p, pd in providers.items() for m in list(pd.get("models", {}))]


def _current_model_id() -> str:
    """当前激活的 `provider/model`（config 模块内存态是唯一真源：/model 刚改过）。"""
    return str(getattr(_llm_config(), "LLM_ACTIVE_ID", "") or "")


def _config_options(catalog: list[tuple[str, str]], active: str) -> list[dict[str, Any]]:
    """把 (catalog, active) 渲染成 schema 的 `SessionConfigOption[]`。

    select 变体的 payload 是 flatten 的（`type` 是判别键）：
      ``{id, name, description?, category?, type:"select", currentValue, options:[{value,name,description?}]}``
    模型名可跨 provider 重名，故 value 一律用 `provider/model`（与 ACTIVE_LLM 同形）。
    """
    if not catalog:
        return []
    options: list[dict[str, Any]] = [{"value": f"{p}/{m}", "name": m, "description": p} for p, m in catalog]
    known = {o["value"] for o in options}
    # currentValue 必须是 options 里的值（客户端按值选中）；激活项不在表里
    # （如 --model/FP_MODEL 覆盖到表外模型）→ 把真实激活值补成一项，
    # 避免 Zed 显示一个选不中的空选择。
    if active and active not in known:
        options.append({"value": active, "name": active, "description": "当前激活（不在配置表中）"})
    current = active or options[0]["value"]
    return [
        {
            "id": _CONFIG_MODEL_ID,
            "name": "Model",
            "description": "LLM 供应点（provider/model）",
            "category": "model",
            "type": "select",
            "currentValue": current,
            "options": options,
        }
    ]


def _context_window_tokens() -> int:
    """fp 的上下文预算（`MAX_CONTEXT_TOKENS`）—— usage_update 的 `size`。

    这是 fp 自己的压缩阈值，不一定等于模型真实窗口：更小是常态（fp 会提前压缩）。
    对客户端而言这才是诚实的「用量分母」——超过它就会触发压缩。
    """
    try:
        return max(256, int(getattr(_llm_config(), "MAX_CONTEXT_TOKENS", 0) or 0))
    except (TypeError, ValueError):
        return 8000


def _encode_cursor(updated: str, sid: str) -> str:
    """分页游标：不透明字符串（规范要求客户端只能原样回传、不得解析）。

    游标里带 (updated, sid) 排序键而非 offset —— 翻页期间若有新会话写入，
    offset 会漏/重，键值游标不会。
    """
    raw = json.dumps([updated, sid], ensure_ascii=False)
    return base64.urlsafe_b64encode(raw.encode()).decode()


def _decode_cursor(cursor: str) -> tuple[str, str] | None:
    """解析游标；非法/被改写（规范禁止，但防一手）→ None（当作第一页）。"""
    try:
        updated, sid = json.loads(base64.urlsafe_b64decode(cursor.encode()).decode())
        return (str(updated), str(sid))
    except Exception:
        return None


@dataclass
class _SessionState:
    """本连接上一条 ACP 会话的**活跃状态**（协议里的 sessionId 是地址，不是当前指针）。

    为什么需要这张表：客户端切 thread 时**不发任何协议消息**（连接没断，
    sessionId 一直有效），它假设 agent 同时记着 N 条对话。旧实现只存一个
    `_session_id`，第二个会话一来就把第一个顶掉 → 两个面板串台、
    回到旧 thread 直接 -32602。

    `cwd` / `busy` / `task` 都按会话隔离：
      - cwd：会话根，相对路径基准（P2 起经 contextvar 生效，不再 chdir 进程）
      - busy：同会话内并发 prompt 属于客户端 bug（跨会话则是排队，见 `_turn_lock`）
      - task：该会话的 turn 任务，session/cancel 只作用于它
    """

    sid: str
    cwd: str
    message_count: int = 0
    busy: bool = False
    task: asyncio.Task[Any] | None = field(default=None, repr=False)


# ── ACP v1 ToolKind 枚举（schema 强约束，只允许这 10 个值）──
# read/edit/delete/move/search/execute/think/fetch/switch_mode/other
# 注意：早期实现返回 "bash"/"analyze" 属非法值，会让客户端 schema 校验失败。
_TOOL_KIND: dict[str, str] = {
    "read_file": "read",
    "write_file": "edit",
    "edit_file": "edit",
    "bash": "execute",
    "python": "execute",
    "file_fingerprint": "read",
    "elf_analysis": "read",
    "web_search": "search",
    "web_fetch": "fetch",
    "subagent": "think",
}

# ── ACP 模式下禁止暴露给 IDE 的毁灭性命令 ──
# 这些命令（exit/exit!/quit）会 raise SystemExit() 终止整个进程，
# IDE 环境中误触会导致 ACP 会话直接断开。
_DESTRUCTIVE_COMMANDS = {"exit", "exit!", "quit"}

# ── 客户端能力（clientCapabilities）的跨 exec 载体 ──
# reload 是 execve 重启进程（内存态全丢），而 Zed 的 stdio 连接保持不变、
# 不会重发 initialize —— 新实例无从得知上一连接协商的能力。故在 initialize 时
# 把原始 capabilities 存入此环境变量（随 execve 继承），新实例启动即恢复。
# 载体模式与 FP_ACP_PENDING_REQ 同路（见 fp_core.core.handoff 模块 docstring）。
_CLIENT_CAPS_ENV = "FP_ACP_CLIENT_CAPS"


# ═══════════════════════════════════════════════════════════
# ACPIO — ACP 专用的 IO 通道
# ═══════════════════════════════════════════════════════════


def _build_form_schema(options: list[str], suggest: str) -> dict[str, Any]:
    """把 ask 的 options/suggest 映射为 elicitation form mode 的受限 JSON Schema。

    form schema 只支持扁平对象 + 原始类型/enum（规范 §Form mode）：
      - 有 options → 单字段 ``answer``(type=string, enum=[...])，天然渲染为选项；
      - suggest 命中 options → 作 default（预填/高亮）；否则仅在自由输入时作 default。
    """
    answer: dict[str, Any] = {"type": "string", "title": "回答"}
    if options:
        answer["enum"] = list(options)
        if suggest and suggest in options:
            answer["default"] = suggest
    elif suggest:
        answer["default"] = suggest
    return {
        "type": "object",
        "properties": {"answer": answer},
        "required": ["answer"],
    }


class ACPIO(IOChannel):
    """
    ACP IO 通道 — 带流式推送的缓冲输出。

    核心设计:
      - 所有 info/item/error/hint/say 调用累积到缓冲区
      - 每累积约 300 字符或显式调用 partial_flush() 时，
        通过 send_chunk 回调推送一条 agent_message_chunk
      - process() 结束后调用 flush_text() 获取剩余文本
      - ask()：客户端在 initialize 声明 elicitation.form 时，走标准
        ``elicitation/create`` 带内提问（accept→回答回流）；否则回退为
        「推送一条展示消息后返回空」（deferred，回答走下一轮）

    为何需要流式？
      长时间运行的 Agent 任务（如代码生成、多步工具调用）中，
      用户需要看到渐进式反馈，而不是静默等待后突然弹出完整回复。
    """

    frontend = "acp"

    # 默认 True（无带内通道）；构造时若注入 elicit 回调 → 实例级置 False。
    # deferred 语义：问题展示给用户后立即返回空，用户下一条消息（新轮次）即其回答。
    # 禁止伪造回答（旧实现返回 "q"）。
    ask_deferred = True

    _STREAM_THRESHOLD = 300

    def __init__(
        self,
        send_chunk: Callable[[str], None] | None = None,
        elicit: Callable[[str, list[str], str], Awaitable[dict[str, Any]]] | None = None,
        send_thought: Callable[[str, str], None] | None = None,
    ):
        """
        Args:
            send_chunk: 可选的回调函数，接收 (text: str)，
                        在缓冲达到阈值时自动推送 agent_message_chunk。
                        同步调用，直接写入 stdout。
            elicit: 可选：ACP 标准 ``elicitation/create`` 的带内提问回调
                    （入参 prompt/options/suggest，返回 elicitation 响应 result）。
                    存在时 ask() 走带内 elicit（accept→回答带内回流），
                    ask_deferred 置 False；缺省或请求失败则回退 deferred 文本推送。
            send_thought: 可选：思考 token 的推送回调，接收 (text, message_id)，
                          推 agent_thought_chunk。缺省时思考流被丢弃（旧行为）。
        """
        self._buffer: list[str] = []
        self._send_chunk: Callable[[str], None] | None = send_chunk
        self._elicit = elicit
        self._char_count = 0
        # ── 思考流（独立缓冲 + 独立 messageId）──
        # 为什么不共用 _buffer：思考与正文是两种 chunk（agent_thought_chunk vs
        # agent_message_chunk），混在一个缓冲里会把推理内容当正文推出去。
        self._send_thought = send_thought
        self._think_buffer: list[str] = []
        self._thought_id: str | None = None
        # 有带内 elicit 通道 → 回答带内回流，非 deferred；否则维持 deferred。
        self.ask_deferred = elicit is None

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
        # 先冲思考再冲正文：思考必须先于最终回答抵达客户端，
        # 否则客户端会把推理展示在回答之后（顺序错乱）。
        self.flush_thought()
        if not self._buffer:
            return ""
        merged = "\n".join(self._buffer)
        self._buffer.clear()
        self._char_count = 0
        return merged

    # ── 思考流（agent_thought_chunk）─────────────────────
    #
    # 契约（ACP v1 ContentChunk）：{content: ContentBlock, messageId?: string}
    # messageId 语义与 agent_message_chunk 相同：同一段思考的所有 chunk 共享
    # 一个 id，id 变化 = 另一段思考。core 每次 LLM 调用会 thinking_start 一次，
    # 多步工具循环里就有多段思考 —— 故 thinking_start 时把 id 置空，懒生成。

    async def thinking_start(self, streaming: bool = False):
        """新一轮思考开始：冲刷上一段残余，并作废旧 id（下一批 token 开新消息）。

        为什么每次 LLM 调用重开一段：多步工具循环里模型会思考多次，
        每段在客户端应显示为独立的思考块，而不是一条无限长的流。
        """
        self.flush_thought()
        self._thought_id = None

    async def thinking_stop(self):
        """思考结束：立即冲刷残余 token（ACP 无「思考结束」通知，靠 chunk 收尾）。

        必须是 async —— core 侧是 ``await io.thinking_stop()``，
        写成同步方法会在 await 处抛 TypeError（非流式 mock 走不到才不易察觉）。
        """
        self.flush_thought()

    def stream_think(self, token: str):
        """接收思考 token；累计到阈值即推送一批 agent_thought_chunk。"""
        if not token or not self._send_thought:
            return
        self._think_buffer.append(token)
        if sum(len(t) for t in self._think_buffer) >= self._STREAM_THRESHOLD:
            self.flush_thought()

    def flush_thought(self) -> None:
        """推送当前思考缓冲（空缓冲不发 —— 不发空 text 块）。"""
        if not self._think_buffer or not self._send_thought:
            self._think_buffer.clear()
            return
        text = "".join(self._think_buffer)
        self._think_buffer.clear()
        if not text:
            return
        if self._thought_id is None:
            self._thought_id = f"tht_{uuid.uuid4().hex[:12]}"
        self._send_thought(text, self._thought_id)

    def _emit_deferred(self, prompt: str, options: list[str] | None, suggest: str) -> None:
        """无带内通道（或 elicit 失败）时的回退：把问题推送为一条展示消息。"""
        lines = [f"❓ {prompt}"]
        for i, opt in enumerate(options or [], 1):
            lines.append(f"   {i}. {opt}")
        if suggest:
            lines.append(f"   💡 推荐默认值：{suggest}")
        lines.append("（请在 IDE 对话框中直接回复此问题）")
        self._accumulate("\n".join(lines))
        self._partial_flush()

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        """向用户提问（结构化契约 v2）。

        两条路径：
          ① 带内 elicit（客户端在 initialize 声明 elicitation.form）：
             发标准 ``elicitation/create``（form mode）并等带内响应 ——
             ``accept`` → 返回 content.answer 文本；``decline``/``cancel``
             → 用户显式拒绝/关闭 → 返回空串（如实告知 LLM，不伪造作答）。
             规范：Agent MUST NOT 假设成功 → 请求/协议异常时**优雅回退** deferred。
          ② 无带内通道 → 既有 deferred：问题推送展示、返回空串，
             回答作为用户的下一条消息在新轮次到达。
        旧实现无条件返回 "q" 会被当成用户回答注入（伪造人类发言），已废弃。
        """
        if self._elicit is not None:
            try:
                result = await self._elicit(prompt, list(options or []), suggest)
            except Exception:  # noqa: BLE001 — 规范：不假设成功，回退展示
                self._emit_deferred(prompt, options, suggest)
                self.ask_deferred = True
                return ""
            action = str(result.get("action") or "")
            if action == "accept":
                content = result.get("content")
                val = cast("dict[str, Any]", content).get("answer") if isinstance(content, dict) else None
                self.ask_deferred = False
                return "" if val is None else str(val).strip()
            # decline / cancel（或未知 action）：用户拒绝或关闭，不注入作答
            self.ask_deferred = False
            return ""
        # 无 elicit → deferred 路径
        self._emit_deferred(prompt, options, suggest)
        self.ask_deferred = True
        return ""

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
      initialize                协议握手，协商版本和能力
      session/new               创建新对话会话（响应带 configOptions）
      session/load              恢复已有会话（**先回放全部历史**再响应）
      session/resume            挂上已有会话（不回放历史；能力位 resume）
      session/delete            删除会话（磁盘历史一并删除；能力位 delete）
      session/list              列出历史会话（按会话自己的 cwd 过滤 + 分页）
      session/close             释放本连接侧会话状态（**不删**历史）
      session/set_config_option 切换配置项（本端只有 model）
      session/prompt            发送用户消息并获取 AI 回复（核心交互）

    实现的通知:
      session/cancel      取消当前操作
      initialized         客户端已就绪

    发出的通知:
      session/update      推送计划/工具调用/回复
                          - agent_message_chunk / agent_thought_chunk（正文 / 思考）
                          - tool_call / tool_call_update（含 locations = follow-along）
                          - plan / available_commands_update
                          - usage_update（上下文占用）、notice（门控 session.notices）
                          - compaction_update（门控 session.compaction）、
                            session_info_update（标题变化）、config_option_update（模型切换）
                          Follow Agent: 文件操作时通知 IDE 打开对应文件（path + line）
    """

    def __init__(self) -> None:
        """
        实例生命周期归 portal.ctl（本类不再持有 Agent 引用）；
        open 在 start() 中调用（构造需 await ensure_initialized）。
        """
        os.environ.setdefault(QUIET_ENV, "1")

        # ── 保存真正的 stdout 引用（JSON-RPC 通道） ──
        self._stdout = sys.stdout

        # ── 会话表（多会话并存）──
        # ACP 一次连接可并存多条会话（各带自己的 cwd/历史），客户端切 thread 时
        # 不发协议消息。所以「当前会话」不是一个指针，而是这张表 + 每条会话的状态。
        self._sessions: dict[str, _SessionState] = {}
        # 最近聚焦的会话（new/load/prompt 更新）：仅用于「客户端没带 sessionId」时的
        # 缺省值，不参与路由判定（路由一律按 params.sessionId 查表）。
        self._session_id: str | None = None
        # 正在跑 turn 的会话：turn 内所有通知（tool_call/plan/chunk）的 sid 来源。
        # core 的执行槽只有一个，故 turn 天然互斥，单变量即准确。
        self._active: _SessionState | None = None
        # ── 跨会话串行闸门（core 只有一个执行槽/一套上下文）──
        # 排队而非拒绝：客户端并发发 prompt 是合法的，先到先跑，
        # 后来者只看到「还在等」（协议允许，没有必须并发的 MUST）。
        self._turn_lock = asyncio.Lock()

        # ── 工具调用追踪（配对 call ↔ result 通知） ──
        self._tool_call_counter = 0
        self._active_tool_call_ids: dict[str, str] = {}

        # ── 工具参数缓存（call → result 阶段共享） ──
        self._last_edit_args: dict[str, Any] | None = None  # edit_file 参数，用于构建 diff
        self._last_read_path: str | None = None  # read_file 路径，用于 result 的 resource URI
        self._last_write_path: str | None = None  # write_file 路径，用于 result 展示内容

        # ── 当前 prompt 处理 task（用于取消） ──
        # 按会话记录：session/cancel 只中断 params.sessionId 那一轮
        # （见 _SessionState.task）。

        # ── 并发 prompt 防护 ──
        # 按会话判定（_SessionState.busy）：同会话并发是客户端 bug（-32600）；
        # 跨会话并发合法，经 _turn_lock 排队，不再全局拒绝。

        # ── agent→client 请求（elicitation 等）：JSON-RPC id → 等待响应 Future ──
        self._pending_requests: dict[int, asyncio.Future[dict[str, Any]]] = {}
        self._req_counter = 0

        # ── 客户端能力（initialize 时协商）：决定 ask_user 是否走带内 elicit ──
        self._client_elicitation: dict[str, bool] = {"form": False, "url": False}
        # 客户端是否声明 session.compaction —— 规范硬约束：未声明则 MUST NOT
        # 发 compaction_update（解析口径见 _apply_client_capabilities）。
        self._client_compaction: bool = False
        # 客户端是否声明 session.notices —— 同上：未声明则 MUST NOT 发 notice
        # （规范允许这种情形用 agent message 代替，见 _send_notice）。
        self._client_notices: bool = False
        # reload 后 Zed 不重发 initialize（stdio 连接不变、进程 exec 重启），
        # 由环境变量恢复上一连接协商的能力（initialize 时写入，见 _apply…）。
        self._restore_client_capabilities()

        # ── 上下文压缩通知（session.compaction）的进程内配对表 ──
        # compactionId 在会话内唯一且**不得复用**：in_progress 与终态必须同 id，
        # 客户端按 id 对同一实体就地打补丁，换 id 会在时间线上变成两条实体。
        self._compaction_ids: dict[str, str] = {}
        # 压缩前的上下文指纹（sid → 指纹）：压缩是否**真的发生**的唯一判据。
        # 不能靠输出文本判断（"对话历史较短，无需压缩" 也是 handled=True）；
        # 也不能只靠条数——compact 在小历史边界上会「1 条换成摘要 + 保留 4 条」，
        # 条数一模一样却是真压缩，条数判据会把它误报成 cancelled。
        self._compaction_fp: dict[str, tuple[int, int]] = {}
        # 每轮开始时的标题快照（sid → summary）：session_info_update 只在真变了才发。
        self._title_before: dict[str, str] = {}
        # 本轮上下文占用（sid → 最近一次 LLM 调用的 prompt_tokens）：
        # usage_update 的 used 来源。由 ON_AFTER_LLM_CALL 钩子写入（见 _on_llm_usage），
        # turn 结束时消费并清空 —— 一轮里的多次工具循环只报最后一次（= 当前上下文长度）。
        self._usage_used: dict[str, int] = {}

        # "Follow Agent" 钩子注册在 start()（须在 portal.ctl.open 之后）

    # ═══════════════════════════════════════════════════════
    # 工具追踪状态管理
    # ═══════════════════════════════════════════════════════

    def _reset_tool_state(self):
        """重置工具调用追踪状态（重载后调用，清除旧 agent 的残留数据）"""
        self._tool_call_counter = 0
        self._active_tool_call_ids.clear()
        self._last_edit_args = None
        self._last_read_path = None
        self._last_write_path = None

    # ═══════════════════════════════════════════════════════
    # 用量上报（usage_update）
    # ═══════════════════════════════════════════════════════

    def _register_usage_hook(self) -> None:
        """用 ON_AFTER_LLM_CALL 抓「最近一次 prompt_tokens」→ usage_update。

        为什么不从 core 取现成值：`TokenTracker` 是**累计**用量（历史所有调用的
        和），拿它当「当前上下文占了多少」会越用越离谱；core 也没有暴露「上下文
        长度」的只读出口。而 `usage.prompt_tokens` 恰好就是「这次请求实际塞了
        多少 token 进上下文」——即客户端要的分母的真实测量值。
        钩子是现成的语义边界，不必为此新增 core 事件。
        """
        portal.ctl.hooks.register(
            LifecycleHook.ON_AFTER_LLM_CALL,
            self._on_llm_usage,
            name="acp_usage_capture",
            priority=1000,
        )

    async def _on_llm_usage(self, ctx: Any, **kwargs: Any) -> Any:
        """记录本会话最近一次 LLM 调用的 prompt_tokens（turn 结束时统一发通知）。

        只在 turn 内记录（`_active` 非空）：turn 之外的隐性调用（总结、标题派生、
        subagent）不属于任何一轮对话，拿它当用量会把数字挂到错误的 thread 上。
        """
        if self._active is None:
            return ctx
        usage = kwargs.get("usage")
        if not isinstance(usage, dict):
            return ctx
        used = cast("dict[str, Any]", usage).get("prompt_tokens")
        if isinstance(used, int) and used > 0:
            self._usage_used[self._active.sid] = used
        return ctx

    # ═══════════════════════════════════════════════════════
    # 上下文压缩通知（session.compaction）
    # ═══════════════════════════════════════════════════════

    # 唯一会在 core 里改写上下文的入口（commands/compact.py）。
    # 注意 ON_BEFORE_COMMAND/ON_COMMAND 的 name 带斜杠（parts[0] 未剥前缀）。
    _COMPACT_COMMAND = "/compact"

    def _register_compaction_hooks(self) -> None:
        """用命令生命周期钩子把「压缩发生」翻译成 ACP compaction_update。

        为什么不改 core：压缩是命令层的策略，core 并没有「上下文被改写」的语义
        事件（compact.py 直接 set_messages），而命令的前后钩子恰好覆盖了
        「开始 / 结束」两个时刻，是现成的语义边界（不必新增事件类型）。
        """
        portal.ctl.hooks.register(
            LifecycleHook.ON_BEFORE_COMMAND,
            self._on_compact_start,
            name="acp_compaction_start",
            priority=1000,
        )
        portal.ctl.hooks.register(
            LifecycleHook.ON_COMMAND,
            self._on_compact_end,
            name="acp_compaction_end",
            priority=1000,
        )

    def _compaction_sid(self) -> str:
        """压缩通知的目标会话（turn 内优先，命令轮一定有 turn）。"""
        return self._turn_sid() or self._session_id or ""

    async def _on_compact_start(self, ctx: Any, **kwargs: Any) -> Any:
        """压缩开始：先发 in_progress（LLM 摘要可能要十几秒，静默等于卡死）。"""
        if kwargs.get("name") != self._COMPACT_COMMAND or not self._client_compaction:
            return ctx
        sid = self._compaction_sid()
        cid = f"cmp_{uuid.uuid4().hex[:12]}"
        self._compaction_ids[sid] = cid
        # 快照必须在 await LLM 之前取：钩子拿到的 `messages` 是会话上下文的
        # **活引用**（agent 传的是 self._conv.messages），压缩后原地变了。
        self._compaction_fp[sid] = self._ctx_fingerprint(kwargs.get("messages"))
        self._send_compaction_update(sid, cid, "in_progress")
        return ctx

    @staticmethod
    def _ctx_fingerprint(messages: object) -> tuple[int, int]:
        """上下文指纹（条数 + 内容哈希）：判断「压缩是否真的改写了上下文」。

        条数单独不够（见 `_compaction_fp` 的注释）；文本判别不可靠（会随文案漂移）。
        直接对内容取哈希，语义就是协议要表达的那件事：上下文变了没有。
        只有在 /compact 轮调用，且上下文至多几百条，开销可忽略。

        入参是钩子给的活引用（`Any` 来源），此处显式窄化：只承诺「能算就行」，
        不去猜消息结构——猜错会在压缩这种低频路径上静默算错指纹。
        """
        h = hashlib.blake2b(digest_size=8)
        msgs: list[Any] = list(cast("list[Any]", messages)) if isinstance(messages, list) else []
        for m in msgs:
            if isinstance(m, dict):
                d: dict[str, Any] = cast("dict[str, Any]", m)
                h.update(str(d.get("role", "")).encode())
                content = d.get("content")
                h.update(b"\x00")
                h.update(content.encode() if isinstance(content, str) else repr(content).encode())
            else:
                h.update(repr(m).encode())
            h.update(b"\x01")
        return (len(msgs), int.from_bytes(h.digest(), "big"))

    async def _on_compact_end(self, ctx: Any, **kwargs: Any) -> Any:
        """压缩结束：按 handled + 上下文指纹是否变化决定终态。"""
        if kwargs.get("name") != self._COMPACT_COMMAND:
            return ctx
        sid = self._compaction_sid()
        cid = self._compaction_ids.pop(sid, None)
        before = self._compaction_fp.pop(sid, None)
        if cid is None:  # 客户端没声明能力 / 无配对的开态：不发，避免孤立实体
            return ctx

        after = self._ctx_fingerprint(kwargs.get("messages"))
        if not kwargs.get("handled"):
            # 压缩失败：error 是**可展示**原因，直接带出命令输出
            self._send_compaction_update(sid, cid, "failed", error=str(kwargs.get("output") or "")[:500])
        elif before is not None and after != before:
            # 上下文真被改写 = completed（含摘要文本为空等边界，只要内容变了）
            self._send_compaction_update(sid, cid, "completed")
        else:
            # handled 但上下文一字未改 = 本来就没东西可压。协议只有四个状态，
            # 用 cancelled 表示「开了但没成事」，不伪造 completed（否则客户端会
            # 在时间线上留一个空压缩实体，用户以为历史被压过）。
            self._send_compaction_update(sid, cid, "cancelled")
        return ctx

    def _send_compaction_update(
        self,
        sid: str,
        compaction_id: str,
        status: str,
        *,
        error: str | None = None,
    ) -> None:
        """发一条 compaction_update（summary 恒省略 = 不改动既有摘要）。

        刻意不发 compaction_summary_chunk / summary：压缩产物的文本由命令层
        拼进 system 消息，ACP 侧没有它的独立副本；伪造一段摘要反而会与
        模型实际看到的上下文不一致。省略即「不修改」，是唯一诚实的选择。
        """
        update: dict[str, Any] = {
            "sessionUpdate": "compaction_update",
            "compactionId": compaction_id,
            "status": status,
        }
        if error:
            update["error"] = error
        self._send_notification("session/update", {"sessionId": sid, "update": update})

    # ═══════════════════════════════════════════════════════
    # 会话元信息通知（session_info_update）
    # ═══════════════════════════════════════════════════════

    def _session_title(self, sid: str) -> str:
        """读取会话标题（meta.summary）；读不到返回空串。

        只用 meta 而不自己派生标题：标题派生策略在 core（跟随末条 user、
        人工重命名锁定后让路），前端复制一份必然漂移。
        """
        try:
            # meta_of(sid) 而非 meta（后者只认当前会话）：跨会话并发时本轮会话
            # 未必是 core 的当前会话，用 meta 会读到别人的标题。
            meta = portal.ctl.sessions.meta_of(sid) or {}
        except Exception as e:  # noqa: BLE001 - 标题是增强，不该拖垮一轮对话
            self._log(f"读取会话标题失败（忽略）: {e}")
            return ""
        return str(meta.get("summary") or "")

    def _notify_session_info(self, sid: str) -> None:
        """标题变了才发 session_info_update（每轮都发会让线程列表无谓重渲染）。"""
        title = self._session_title(sid)
        before = self._title_before.pop(sid, "")
        if not title or title == before:
            return
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "session_info_update",
                    "title": title,
                    # 规范要求 ISO 8601（带时区）；本地 naive 时间会被判非法
                    "updatedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
                },
            },
        )

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

        # ── 工具调用前：提取文件路径，通知 IDE 打开 ──
        portal.ctl.hooks.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            name="acp_follow_tool_call",
            priority=1000,  # 高优先级
        )

        # ── 工具完成后：通知 IDE 工具已完成 ──
        portal.ctl.hooks.register(
            LifecycleHook.ON_TOOL_RESULT,
            self._on_tool_result,
            name="acp_follow_tool_result",
            priority=1000,
        )

    @staticmethod
    def _line_of(file_path: str, needle: str) -> int | None:
        """needle 在文件里首次出现的行号（1-based）；找不到/读不了返回 None。

        为什么按内容找而不是按参数算：`edit_file` 只给 old_string/new_string，
        真正的落点只能由文件内容决定。`read_file` 的 offset 是 0-based，换算在
        调用处（见 `_extract_location`），两种语义不要混在一个函数里。

        needle 可能是多行片段 → 用 find(count '\\n') 而不是逐行比对。
        只用于「Follow 跳到哪一行」这种提示性场景：读不了/文件过大就返回 None，
        让调用方只发 path —— 定位失败绝不能拖垮工具调用本身。
        """
        if not needle:
            return None
        try:
            with open(file_path, encoding="utf-8", errors="replace") as f:
                text = f.read(_LOCATE_MAX_BYTES)
        except OSError:
            return None
        idx = text.find(needle)
        if idx < 0:
            return None
        return text.count("\n", 0, idx) + 1

    def _extract_location(self, tool_name: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """从工具调用参数中提取 ToolCallLocation（`path` + 可选 `line`，1-based）。

        line 是 Zed follow-along 真正需要的字段：只给 path 时编辑器能打开文件但
        落到哪一行全凭它自己猜（通常落到第 1 行或上次光标处），于是「跟随 Agent」
        在长文件里形同虚设。

        支持的工具与行号来源:
          read_file   → offset（0-based）→ line = offset + 1
          edit_file   → old_string 在文件里的首行（**编辑前**定位）
          write_file / file_fingerprint / elf_analysis → 只给 path
          bash        → 从命令里猜路径，不给行号（命令行里的行语义不可靠）

        参数 args 应为已解析为 dict 的参数字典，由调用方保证。
        """
        if not isinstance(args, dict):  # type: ignore[reportUnnecessaryIsInstance]
            return None

        # 直接有 file_path 参数的
        fp = args.get("file_path")
        if fp and isinstance(fp, str) and os.path.isfile(fp):
            path = os.path.abspath(fp)
            line: int | None = None
            if tool_name == "read_file":
                off = args.get("offset")
                if isinstance(off, int) and off >= 0:
                    line = off + 1
            elif tool_name == "edit_file":
                old = args.get("old_string")
                if isinstance(old, str):
                    line = self._line_of(path, old)
            loc: dict[str, Any] = {"path": path}
            if line:
                loc["line"] = line
            return loc

        # bash 命令中尝试提取文件路径
        if tool_name == "bash":
            cmd = args.get("command", "")
            if cmd:
                match = self._FILE_PATH_PATTERN.search(cmd)
                if match:
                    potential = os.path.abspath(os.path.expanduser(match.group(1)))
                    if os.path.isfile(potential):
                        return {"path": potential}

        return None

    def _get_tool_kind(self, tool_name: str) -> str:
        """返回 ACP v1 合法 ToolKind（read/edit/delete/move/search/execute/think/fetch/switch_mode/other）。"""
        return _TOOL_KIND.get(tool_name, "other")

    async def _on_tool_call(
        self,
        context: HookContext,
        tool_name: str = "",
        tool_args: str = "",
        tool_call_id: str = "",
        **kwargs: Any,
    ) -> None:
        """工具调用前 — 发送 tool_call 通知

        tool_call_id 由 fp-core 传入（LLM 生成的 call id，与 ON_TOOL_RESULT 同值），
        直接用作 ACP 的 toolCallId → call/result 天然配对，并行同名工具不会互相错配。
        """
        # ── 会话未就绪时跳过（start() 之前触发的钩子） ──
        sid = self._turn_sid()
        if sid is None:
            return

        # ── 解析参数 ──
        args_dict: dict[str, Any] = {}
        with contextlib.suppress(json.JSONDecodeError, TypeError):
            args_dict = json.loads(tool_args) if isinstance(tool_args, str) else {}  # type: ignore[reportUnnecessaryIsInstance]

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
        location = self._extract_location(tool_name, args_dict)
        file_path = str(location["path"]) if location else None
        title = self._build_tool_title(tool_name, args_dict, kind)
        # ── 构建 ToolCall（ACP v1 schema）──
        # content 是 ToolCallContent 数组，元素必须是 union 变体：
        #   {"type":"content","content":<ContentBlock>} / {"type":"diff",...} / {"type":"terminal",...}
        # 旧实现直塞 {"type":"resource",...}（ContentBlock）属于层级错误，严格客户端会丢弃整条 update。
        content: list[dict[str, Any]] = [
            {
                "type": "content",
                "content": {"type": "text", "text": self._build_human_description(tool_name, args_dict)},
            }
        ]
        # follow-along：locations 才是规范的「文件位置」通道（绝对路径）
        # location 里带了 line 就必须原样带上：ToolCallLocation.line 是 1-based，
        # 缺省时 IDE 只能自己猜落点，长文件里等于没用。
        locations: list[dict[str, Any]] = []
        if location and file_path and os.path.isfile(file_path):
            locations.append(location)
            content.append({
                "type": "content",
                "content": {
                    "type": "resource",
                    "resource": {"uri": f"file://{file_path}", "mimeType": self._guess_mime(file_path)},
                },
            })

        update: dict[str, Any] = {
            "sessionUpdate": "tool_call",
            "toolCallId": self._tool_call_id(tool_name, tool_call_id),
            "title": title,
            "kind": kind,
            "status": "in_progress",
            # rawInput 的 schema 类型是 object = 工具原始参数（不是格式化字符串）
            "rawInput": args_dict,
            "content": content,
        }
        if locations:
            update["locations"] = locations

        self._send_notification("session/update", {"sessionId": sid, "update": update})

    # ── 工具标题和 input 构建 ────────────────────────────

    # Emoji 分类，让 IDE 的 tool_call 面板一目了然（键为 ACP v1 合法 ToolKind）
    _TOOL_EMOJI: dict[str, str] = {
        "read": "📖",
        "edit": "✏️",
        "execute": "🖥️",
        "search": "🔍",
        "fetch": "🌐",
        "think": "🧠",
        "other": "🔧",
    }

    def _build_tool_title(self, tool_name: str, args: dict[str, Any], kind: str) -> str:
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

    def _build_human_description(self, tool_name: str, args: dict[str, Any]) -> str:
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
                    result_str = cast(str, parsed["result"])
        return result_str

    async def _on_tool_result(
        self,
        context: HookContext,
        tool_name: str = "",
        result: Any = "",
        tool_call_id: str = "",
        **kwargs: Any,
    ) -> None:
        """工具完成后 — 发送 tool_call_update

        rawOutput 在 schema 里是 object（原始返回值），工具结果是字符串 →
        一律不填 rawOutput，展示内容走 content（ToolCallContent union 的正规通道）。
        """

        # ── 会话未就绪时跳过 ──
        sid = self._turn_sid()
        if sid is None:
            return

        status = "failed" if "❌" in str(result)[:10] else "completed"
        result_str = str(result)

        # ── 清理结果：subagent 抽取 result 字段 ──
        display_str = self._extract_display_result(tool_name, result_str)

        # ── content：ToolCallContent union 数组（元素 type ∈ content/diff/terminal）──
        def _text_block(text: str) -> dict[str, Any]:
            """标准 ContentBlock 包装成 ToolCallContent（resource 保留换行，text 会塌成单行）。"""
            return {
                "type": "content",
                "content": {
                    "type": "resource",
                    "resource": {"uri": "inline://tool-output", "mimeType": "text/plain", "text": text},
                },
            }

        content_blocks: list[dict[str, Any]] = []

        # 所有工具的结果统一用 resource/text/plain 展示（保留换行和缩进）
        # read_file 跳过——它用专用 resource 代码块展示文件内容
        if display_str and tool_name not in ("read_file",):
            content_blocks.append(_text_block(display_str))

        # read_file 额外加 resource 代码块
        if tool_name == "read_file" and status != "failed":
            read_path = self._last_read_path or "(inline)"
            mime = self._guess_mime(read_path) if read_path != "(inline)" else "text/plain"
            content_blocks.append({
                "type": "content",
                "content": {
                    "type": "resource",
                    "resource": {"uri": f"file://{read_path}", "mimeType": mime, "text": result_str},
                },
            })

        # write_file 额外加 resource 展示写入内容
        if tool_name == "write_file" and status != "failed" and self._last_write_path:
            write_path = self._last_write_path
            if os.path.isfile(write_path):
                with open(write_path, encoding="utf-8", errors="replace") as f:
                    file_content = f.read()
                content_blocks.append({
                    "type": "content",
                    "content": {
                        "type": "resource",
                        "resource": {
                            "uri": f"file://{write_path}",
                            "mimeType": self._guess_mime(write_path),
                            "text": file_content,
                        },
                    },
                })

        # edit_file 加 diff 块（ToolCallContent.diff；path 必须是绝对路径）
        if tool_name == "edit_file" and status == "completed" and self._last_edit_args:
            args = self._last_edit_args
            old_text = args.get("old_string", "")
            new_text = args.get("new_string", "")
            file_path = args.get("file_path", "")
            if old_text and new_text and file_path:
                content_blocks.append({
                    "type": "diff",
                    "path": os.path.abspath(str(file_path)),
                    "oldText": old_text,
                    "newText": new_text,
                })

        update: dict[str, Any] = {
            "sessionUpdate": "tool_call_update",
            "toolCallId": self._tool_call_id_for_result(tool_name, tool_call_id),
            "status": status,
        }
        if content_blocks:
            update["content"] = content_blocks

        # ── locations 的三态补丁语义（schema 原文）──
        #   省略 = 保持不变 ；null/[] = 清空 ；非空 = 全量替换
        # 这里默认**省略**：call 阶段已经发过正确的 path(+line)，重复发只会让
        # IDE 重算光标。唯一需要补发的是「call 阶段文件还不存在」的情形 ——
        # write_file 新建文件时 os.path.isfile 为假、locations 为空，此时 IDE 里
        # 这条工具没有 follow 链接；文件落盘后才补一条，且不带 line（新文件无落点语义）。
        #
        # 为什么不对 edit_file 补发：编辑后 old_string 可能已被移动/消失，也可能在
        # 别处重复出现 —— 重新按内容定位会算出**错误**行号，把 follow 光标带偏。
        # 定位错了比不定位更糟，所以 edit_file 一律沿用 call 阶段的定位。
        if tool_name == "write_file" and status == "completed" and self._last_write_path:
            wp = os.path.abspath(self._last_write_path)
            if os.path.isfile(wp):
                update["locations"] = [{"path": wp}]

        self._send_notification("session/update", {"sessionId": sid, "update": update})

    def _tool_call_id(self, tool_name: str, tool_call_id: str = "") -> str:
        """toolCallId：优先用 fp-core 传入的真实 call id（与 ON_TOOL_RESULT 同值）→ 天然配对。

        旧实现以 tool_name 为键缓存，并行同名调用（如两个 bash）会互相覆盖，
        导致第一条的 result 更新到第二条 tool_call 上。现按真实 id 配对。
        """
        if tool_call_id:
            return tool_call_id
        self._tool_call_counter += 1
        call_id = f"fp_{tool_name}_{self._tool_call_counter}"
        self._active_tool_call_ids[tool_name] = call_id
        return call_id

    def _tool_call_id_for_result(self, tool_name: str, tool_call_id: str = "") -> str:
        """result 阶段取配对 id（与 call 阶段同一来源；缺失时退回同名最近一次）。"""
        if tool_call_id:
            return tool_call_id
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

    async def _reload_continue(self) -> None:
        """reload handoff 续接（机制契约见 fp_core.core.handoff，acp 入口适配）。

        旧实例 exec 重启时正有一个 session/prompt 请求挂起（req_id 存于
        环境变量 FP_ACP_PENDING_REQ）。两种 kind 都必须为其补发 result，
        否则客户端悬等：
        - kind=tool：注入完成的会话直接跑完一轮 → 流式 chunk 走 session/update
          通知 → 为挂起请求补发 result，客户端无感知地看到对话自然收尾；
        - kind=command：会话已恢复，显示完成提示即回 read_loop。
        本方法在进 stdin read_loop 之前执行，期间客户端的新请求在管道里排队。
        """
        if not os.environ.get("FP_RELOAD_HANDOFF"):
            return
        _directive = portal.ctl.take_reload()
        if _directive.notice:
            self._log(_directive.notice)

        pending_raw = os.environ.pop("FP_ACP_PENDING_REQ", None)
        pending_req: Any = None
        if pending_raw is not None:
            try:
                pending_req = json.loads(pending_raw)
            except (ValueError, TypeError):
                pending_req = None

        if not _directive.should_continue:
            # 命令面：无续接轮，只为挂起请求补发 result 防客户端悬等
            if pending_req is not None:
                self._send_result(pending_req, {"stopReason": "end_turn"})
                self._log("🔄 已为挂起的请求补发 result（command 面）")
            return

        sid = self._session_id
        # 续接的这一轮同样要进会话表：turn 内的 tool_call/chunk 通知靠 _turn_sid
        # 取 sid（不进表就会被静默丢掉），session/cancel 也靠表定位要中断的那一轮。
        st = self._adopt_session(sid) if sid else None
        acp_io = self._make_acp_io(sid)
        self._log("🔄 reload 续接：恢复会话并继续上一轮对话…")
        try:
            self._active = st
            response = await portal.run.continue_(io=acp_io)
            buf = acp_io.flush_text()
            reply_text = response.content or ""
            if buf.strip():
                self._send_message_notification(buf, session_id=sid)
            elif reply_text:
                self._send_message_notification(reply_text, session_id=sid)
            if pending_req is not None:
                # 为 exec 前挂起的 session/prompt 补发 result，解除客户端等待
                self._send_result(pending_req, {"stopReason": "end_turn"})
                self._log("🔄 已为挂起的 prompt 请求补发 result")
        finally:
            self._active = None

    async def start(self) -> None:
        """启动 ACP 服务器，从 stdin 读取 JSON-RPC 消息"""
        # 实例创建（print/display 重定向到 stderr，stdout 是 JSON-RPC 通道）
        with contextlib.redirect_stdout(sys.stderr):
            await portal.ctl.open(enable_log=False, resume=os.environ.get("FP_RELOAD_SID") or None)
        self._register_follow_hooks()
        self._register_compaction_hooks()
        self._register_usage_hook()
        self._session_id = portal.run.status.session_id
        # 启动即持有的会话（reload 恢复的那个）先入表：客户端可能不给 load
        # 直接发 prompt（连接没断，它认为会话一直活着）。
        if self._session_id:
            self._adopt_session(self._session_id)

        # ── reload handoff：在进入消息循环前完成上一轮对话的续接 ──
        await self._reload_continue()

        self._log(f"✅  ACP Server 启动 (session={self._session_id})")
        self._log(f"   模型: {portal.run.status.model}")

        # ── 设置异步 stdin 读取器 ──
        loop = asyncio.get_running_loop()
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

    async def _read_loop(self, reader: asyncio.StreamReader) -> None:
        """从 stdin 逐行读取 JSON-RPC 消息（全异步派发）

        关键设计：所有请求/通知都通过 create_task 异步派发，不阻塞 stdin 读取循环。
        这样 session/cancel 通知可以在 prompt 处理期间被读取和派发，
        实现对正在运行的 agent 真正中断。
        """
        active_tasks: set[asyncio.Task[Any]] = set()

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
                request: dict[str, Any] = json.loads(text)
            except json.JSONDecodeError as e:
                self._log(f"⚠️  JSON 解析失败: {e}")
                continue

            # 创建独立 task 派发，不阻塞 stdin 读取
            task = asyncio.create_task(self._dispatch(request))
            active_tasks.add(task)
            task.add_done_callback(active_tasks.discard)

    async def _dispatch(self, request: dict[str, Any]) -> None:
        """分发 JSON-RPC 请求到对应的处理器"""
        # ── 响应（有 id、无 method）：agent→client 请求（如 elicitation）的回执 ──
        if "method" not in request and "id" in request:
            self._resolve_pending(request)
            return

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
            # ── reload handoff：记录挂起的 prompt 请求 ID（exec 重启后由
            #    新实例 _reload_continue 补发 result，客户端不会悬等） ──
            if method == "session/prompt":
                os.environ["FP_ACP_PENDING_REQ"] = json.dumps(req_id)

            with contextlib.redirect_stdout(sys.stderr):
                result = await handler(params)

            self._send_result(req_id, result)
            if method == "session/prompt":
                os.environ.pop("FP_ACP_PENDING_REQ", None)

            # session/new、session/load 和 session/resume 之后，等待 IDE 完全消化
            # 响应并注册 sessionId，然后再发送 available_commands_update 通知。直接
            # 连续发送会导致 Zed 在处理通知时 session 尚未注册，报 "unknown session"
            # 并丢弃命令列表。（resume 同样建立了一条「可寻址」的会话，客户端切到它
            # 之后必须能打斜杠命令 —— 漏掉它会让切回来的 thread 命令全失效。）
            if method in ("session/new", "session/load", "session/resume"):
                await asyncio.sleep(0.05)
                self._send_available_commands()
        except asyncio.CancelledError:
            # session/prompt 的 result 必须含 stopReason（null 非法）；其余方法
            # （如 reload handoff 探针）无合法 result 定义，保留 null 语义。
            if method == "session/prompt":
                self._send_result(req_id, {"stopReason": "cancelled"})
            else:
                self._send_result(req_id, None)
        except _ACPRPCError as e:
            self._log(f"⚠️  {method} 协议错误 [{e.code}]: {e.message}")
            self._send_error(req_id, e.code, e.message)
        except Exception as e:
            self._log(f"❌  处理 {method} 失败: {e}")
            traceback.print_exc(file=sys.stderr)
            self._send_error(req_id, -32603, str(e))

    def _get_handler(self, method: str) -> Callable[[dict[str, Any]], Awaitable[dict[str, Any]]] | None:
        """根据方法名查找对应的处理器"""
        handlers: dict[str, Callable[[dict[str, Any]], Awaitable[dict[str, Any]]]] = {
            "initialize": self._handle_initialize,
            "session/new": self._handle_session_new,
            "session/load": self._handle_session_load,
            "session/prompt": self._handle_session_prompt,
            "session/list": self._handle_session_list,
            "session/close": self._handle_session_close,
            "session/resume": self._handle_session_resume,
            "session/delete": self._handle_session_delete,
            "session/set_config_option": self._handle_session_set_config_option,
            "session/commands": self._handle_session_commands,
            "session/ping": self._handle_session_ping,
        }
        return handlers.get(method)

    async def _handle_notification(self, method: str, params: dict[str, Any]) -> None:
        """处理通知（无 id 的请求）"""
        if method == "session/cancel":
            self._handle_cancel(params)
        elif method == "initialized":
            self._log("客户端已初始化")
            # 不在此处注册命令——此时只有默认 sessionId（IDE 不认识它），
            # 命令由 session/new 和 session/load handler 在正确的会话中注册。
        else:
            pass

    # ═══════════════════════════════════════════════════════
    # ACP v1 方法处理器
    # ═══════════════════════════════════════════════════════

    def _apply_client_capabilities(self, caps: dict[str, Any]) -> None:
        """解析并记录客户端能力（initialize 协商与 reload 恢复共用的唯一解析点）。

        规范：``elicitation.form``/``url`` 各自「显式存在且非 null」才算声明；
        缺失/null = 不支持。
        """
        elicit = caps.get("elicitation")
        elicit_d = cast("dict[str, Any]", elicit) if isinstance(elicit, dict) else {}
        self._client_elicitation = {
            "form": elicit_d.get("form") is not None,
            "url": elicit_d.get("url") is not None,
        }
        # session.compaction：schema 里是 Option<CompactionCapabilities>（结构体），
        # 故只有「值是对象」才算声明。schema 的 serde 对 false/true/"supported"
        # 这类噪声一律解析成 None（= 不支持），本端同口径：非 dict 即视为未声明。
        sess = caps.get("session")
        sess_d = cast("dict[str, Any]", sess) if isinstance(sess, dict) else {}
        self._client_compaction = isinstance(sess_d.get("compaction"), dict)
        # session.notices：同样是 Option<NoticesCapabilities>（空结构体），
        # 只有「值是对象」才算声明。未声明时 MUST NOT 发 notice。
        self._client_notices = isinstance(sess_d.get("notices"), dict)

    def _restore_client_capabilities(self) -> None:
        """reload 后从环境变量恢复连接级客户端能力（新实例收不到 initialize）。

        仅在环境变量存在（= 本进程由 reload 从上一实例 exec 而来）时生效；
        全新连接不会有该变量，能力仍由 initialize 正常协商。损坏则保持默认。
        """
        raw = os.environ.get(_CLIENT_CAPS_ENV)
        if not raw:
            return
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            return
        if isinstance(parsed, dict):
            self._apply_client_capabilities(cast("dict[str, Any]", parsed))

    async def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ACP v1 协议握手。

        参考: https://agentclientprotocol.com/protocol/v1/initialization
        """
        client_info = params.get("clientInfo", {})
        self._log(f"客户端连接: {client_info.get('name', '?')} v{client_info.get('version', '?')}")

        # ── 记录客户端能力（标准能力协商，非任何客户端特化）──
        caps = params.get("clientCapabilities")
        caps_d = cast("dict[str, Any]", caps) if isinstance(caps, dict) else {}
        self._apply_client_capabilities(caps_d)
        # 跨 exec 持久化：reload 后本进程被 execve 替换、Zed 不重发 initialize，
        # 新实例只能靠此环境变量恢复能力（载体模式同 FP_ACP_PENDING_REQ）。
        os.environ[_CLIENT_CAPS_ENV] = json.dumps(caps_d, ensure_ascii=False)
        self._log(f"客户端 elicitation: form={self._client_elicitation['form']} url={self._client_elicitation['url']}")

        # ── 协议版本协商 ──
        # 客户端声明它支持的版本，agent 回自己能用的版本（不匹配由客户端决定
        # 是否继续）。此前无条件回本端版本，等于静默无视客户端声明。
        requested = params.get("protocolVersion")
        negotiated = (
            requested
            if isinstance(requested, int) and requested in _SUPPORTED_PROTOCOL_VERSIONS
            else ACP_PROTOCOL_VERSION
        )
        if requested is not None and negotiated != requested:
            self._log(f"协议版本协商: 客户端请求 v{requested}，本端仅支持 v{negotiated}")

        return {
            "protocolVersion": negotiated,
            "agentCapabilities": {
                # 已实现历史回放（session/load 先回放 user/agent chunk 再响应），
                # 故诚实声明 True。
                "loadSession": True,
                "sessionCapabilities": {
                    "list": {},
                    "close": {},
                    # resume：挂上已有会话但**不回放**历史（大历史切 thread 不必等回放）
                    "resume": {},
                    # delete：连磁盘历史一起删（close 只释放本连接侧状态，不删历史）
                    "delete": {},
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
                "name": "fp",
                "title": "FP",
                "version": _agent_version(),
            },
            "authMethods": [],
        }

    # ═══════════════════════════════════════════════════════
    # 会话表与路由（多会话并存）
    # ═══════════════════════════════════════════════════════

    def _turn_sid(self) -> str | None:
        """当前 turn 的会话 id（通知路由的唯一真源）。

        turn 内的 tool_call / plan / chunk 通知必须带**该轮自己的** sid：
        用「最近聚焦会话」会在用户切 thread 后把 A 的输出打到 B 的面板上。
        turn 之外（如 job 里的工具）回落聚焦会话——此时没有更好的真源。
        """
        if self._active is not None:
            return self._active.sid
        return self._session_id

    def _validate_session_cwd(self, cwd: Any, method: str) -> str:
        """校验会话根目录，**不产生任何副作用**（不 chdir、不动 core）。

        与 `_enter_session` 分开的意义：cwd 合法性是请求级校验，可以立刻
        回答客户端；而「进入会话」会改写 core 的上下文，必须串行（见 _turn_lock）。
        旧实现把两者焊在一起（_apply_session_cwd），于是 session/new 也得抢执行槽。
        """
        if not isinstance(cwd, str) or not cwd:
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"{method} 缺少必填参数 cwd（会话根目录绝对路径）")
        target = os.path.abspath(cwd)
        if not os.path.isdir(target):
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"会话 cwd 不存在或不是目录: {cwd}")
        return target

    def _adopt_session(self, sid: str, cwd: str | None = None) -> _SessionState | None:
        """把一条**已存在**的会话纳入会话表（幂等）；来源按可信度排序。

        收养顺序：本次连接登记过 → 客户端声明的 cwd → 磁盘 meta 的 cwd →
        core 当前会话（reload/内存态）。都证明不了就返回 None（调用方报 -32602）。

        为什么允许「没 load 过就收养」：客户端切 thread 不发消息，agent 进程
        重启后它也不会补发 load，但它手上的 sessionId 仍然有效。
        """
        st = self._sessions.get(sid)
        if st is not None:
            if cwd:  # 客户端当下声明是唯一可信来源（项目可能被移动）
                st.cwd = cwd
            return st

        known_cwd: str | None = cwd
        if known_cwd is None:
            try:
                meta = portal.ctl.sessions.list().get(sid) or {}
                raw = meta.get("cwd")
                known_cwd = raw if isinstance(raw, str) and raw else None
            except Exception:
                known_cwd = None
        if known_cwd is None:
            if sid != portal.run.status.session_id:
                return None
            known_cwd = current_cwd()  # 兜底：core 当前会话所属上下文（P2 起不再是进程 cwd）
        st = _SessionState(sid=sid, cwd=os.path.abspath(known_cwd))
        self._sessions[sid] = st
        return st

    def _resolve_session(self, params: dict[str, Any], method: str) -> _SessionState:
        """按 params.sessionId 路由；缺省回落到最近聚焦会话（老客户端兼容）。"""
        raw = params.get("sessionId") or params.get("session_id")
        sid = raw if isinstance(raw, str) and raw else self._session_id
        if not sid:
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"{method} 缺少必填参数 sessionId")
        st = self._sessions.get(sid)
        if st is None:
            raise _ACPRPCError(
                _RPC_INVALID_PARAMS,
                f"未知会话: {sid}（本连接已登记的会话: {sorted(self._sessions) or '无'}）",
            )
        return st

    def _enter_session(self, st: _SessionState) -> Token[str | None] | None:
        """**进入**一条会话：core 上下文切到它、cwd 成为相对路径基准。

        返回 cwd 的复位令牌，调用方（turn 循环）必须在同一 Task 内复位。
        必须在 `_turn_lock` 内调用：core 只有一个执行槽/一套上下文，
        并发进入会让两条会话的消息写进同一个 conversation。

        为什么不把「切 core」放在 session/new：客户端常在 A 还在输出时
        开新 thread（Zed 的 New Thread），此时切 core 会把 A 的对话对象
        重置，A 这一轮的回答就落到 B 的历史里。
        """
        cur = portal.run.status.session_id
        if cur != st.sid:
            info = portal.ctl.sessions.load(st.sid)
            if info is None:
                # 盘上还没有它（session/new 后没产生过消息 → 文件惰性创建）。
                # 用预分配的 sid 建档：core 的 create_session 接受指定 sid，
                # 这样 cwd 能随会话落盘，session/list 才过滤得准。
                info = portal.ctl.sessions.new(cwd=st.cwd, sid=st.sid)
            self._log(f"进入会话: {st.sid}（cwd={st.cwd}）")
        return self._session_cwd_apply(st)

    def _session_cwd_apply(self, st: _SessionState) -> Token[str | None] | None:
        """让相对路径基准 = 会话 cwd（P2：contextvar，**不再 chdir**）。

        `os.chdir` 是进程级全局可变状态，用它承载会话 cwd 意味着正确性只能靠
        「同一时刻只有一个会话在跑」这条纪律。改成 `fp_core.core.cwd` 的
        ContextVar 后正确性来自语言机制（按 Task 隔离），即使将来放开并发
        也不会把 A 的相对路径解析到 B 的仓库里。

        **通常不需要它**：core 的 `_load_meta_from_session` 在装载会话时已把
        基准绑到 `meta.cwd`（cwd 是会话属性）。这里只处理一种偏离——客户端在
        `session/load` 里声明的 cwd 与落盘 meta 不一致时，以客户端为准做**本轮**
        覆盖。返回 None 表示无需复位。
        """
        target = os.path.abspath(st.cwd)
        if current_cwd() == target:
            return None
        return set_cwd(target)

    def _handle_cancel(self, params: dict[str, Any]) -> None:
        """取消**指定会话**的那一轮（cancel 通知也算 session-scoped 操作）。

        旧实现无脑 `portal.run.cancel()`：一旦有多条会话，取消 B 会把正在跑的
        A 也打断。故只在「被取消的正是当前执行槽持有者」时才置 core 中断标志。
        """
        raw = params.get("sessionId") or params.get("session_id")
        sid = raw if isinstance(raw, str) and raw else self._session_id
        st = self._sessions.get(sid) if sid else None
        if st is None:
            self._log(f"⏹️  忽略未知会话的取消: {sid}")
            return
        self._log(f"⏹️  收到取消通知（会话 {st.sid}），正在中断 agent...")

        task = st.task
        if task is not None and not task.done():
            task.cancel()
            self._log("  → 已取消该会话的处理 task")
        # core 中断标志是全局的：只有取消「正在跑的那一轮」时才能碰，
        # 否则会误伤另一条会话正在执行的 turn。
        if self._active is st:
            portal.run.cancel()

    def _replay_history(self, session_id: str) -> None:
        """把历史会话回放给客户端（session/load 响应之前必须完成）。

        规范 MUST：load 时用 session/update 把整个会话流给客户端，否则客户端
        本地状态为空、与 agent 侧上下文不一致（此前只回 sessionId 就响应）。
        system/tool 消息不回放；同一角色的连续片段各自成一条消息（messageId 不同），
        符合「messageId 变化即新消息」的约定。
        """
        try:
            history = portal.ctl.sessions.read_messages(session_id) or []
        except Exception as e:  # 读取失败不应让 load 整体失败
            self._log(f"读取会话历史失败，跳过回放: {e}")
            return
        replayed = 0
        for msg in history:
            role = msg.get("role")
            content = msg.get("content")
            if not isinstance(content, str) or not content.strip():
                continue
            if role == "user":
                kind = "user_message_chunk"
            elif role == "assistant":
                kind = "agent_message_chunk"
            else:
                continue
            self._send_notification(
                "session/update",
                {
                    "sessionId": session_id,
                    "update": {
                        "sessionUpdate": kind,
                        "messageId": str(uuid.uuid4()),
                        "content": {"type": "text", "text": content},
                    },
                },
            )
            replayed += 1
        self._log(f"会话历史回放: {replayed} 条")

    # ── 客户端转发的 MCP server（会话私有）────────────────

    def _register_session_mcp(self, sid: str, params: dict[str, Any]) -> None:
        """把客户端经 `session/new`|`session/load` 转发的 `mcpServers` 登记为**会话私有**。

        方案 B（会话私有）：IDE（如 Zed 的 Settings → MCP Servers）里配的 server
        只对声明它的会话可见，不污染其它会话的工具面。可见性由插件侧 `_visible()`
        从执行期 contextvar 取 session id 过滤 —— 与 cwd 同一范式。

        本端只支持 **stdio** 传输；`http`/`sse` 型由插件明确拒绝并记日志，
        不静默忽略（静默是本项目反复踩过的失败模式）。
        """
        # schema 里 mcpServers 是**数组**（stdio 元素 untagged、env 是 [{name,value}]），
        # 旧实现按 dict 解析 → 真实客户端流量整包被丢。归一化见 _normalize_mcp_servers。
        servers, rejected = _normalize_mcp_servers(params.get("mcpServers"))
        if not servers and not rejected:
            return
        if not servers:
            self._log(f"mcpServers 全部无法使用: {rejected}")
            self._send_notice("warning", "MCP server 未启用", "；".join(rejected))
            return
        try:
            plug: Any = portal.ctl.plugins.get(_MCP_PLUGIN_NAME)
        except Exception as e:
            self._log(f"客户端传入 {len(servers)} 个 mcpServers，但取不到 mcp 插件（{e}），已忽略")
            self._send_notice("warning", "MCP server 未启用", f"未加载 mcp 插件：{e}")
            return
        if plug is None or not hasattr(plug, "add_session_servers"):
            self._log(f"客户端传入 {len(servers)} 个 mcpServers，但本端未加载 mcp 插件，已忽略")
            self._send_notice("warning", "MCP server 未启用", "本端未加载 mcp 插件")
            return
        try:
            rejected += plug.add_session_servers(sid, servers) or []
        except Exception as e:
            self._log(f"mcpServers 登记失败: {type(e).__name__}: {e}")
            self._send_notice("error", "MCP server 登记失败", f"{type(e).__name__}: {e}")
            return
        msg = f"会话 {sid} 登记 MCP server: {sorted(servers)}"
        if rejected:
            self._log(f"⚠️ {msg}；拒绝: {rejected}")
            self._send_notice("warning", "部分 MCP server 被拒绝", "；".join(rejected))
        else:
            self._log(msg)

    def _drop_session_mcp(self, sid: str) -> None:
        """回收会话私有 MCP server（杀子进程）。未加载插件时静默跳过。"""
        try:
            plug: Any = portal.ctl.plugins.get(_MCP_PLUGIN_NAME)
            if plug is not None and hasattr(plug, "drop_session_servers"):
                plug.drop_session_servers(sid)
        except Exception as e:  # noqa: BLE001 - 清理失败不该阻止会话关闭
            self._log(f"回收 {sid} 的 MCP server 失败（忽略）: {e}")

    def _evict_sessions(self) -> None:
        """淘汰最久未用的会话登记，防 `_sessions` 只增不减（长连接跑几十个 thread 会泄漏）。

        跳过当前聚焦会话与 busy（正在输出）会话；淘汰只清**本连接侧**状态 +
        会话私有 MCP，**不删磁盘历史** —— 客户端再次 session/load 即可恢复。
        """
        while len(self._sessions) > _MAX_SESSIONS:
            victim = next(
                (s for s, st in self._sessions.items() if s != self._session_id and not st.busy),
                None,
            )
            if victim is None:
                return
            self._sessions.pop(victim, None)
            self._drop_session_mcp(victim)
            self._log(f"淘汰会话登记: {victim}（超上限 {_MAX_SESSIONS}）")

    async def _handle_session_close(self, params: dict[str, Any]) -> dict[str, Any]:
        """ACP v1 关闭会话：释放本连接侧状态与会话私有 MCP server。

        与 session/list 的 `delete` 不是一回事：close **不删磁盘历史**，
        用户的对话仍然可以用 session/load 再打开。幂等（重复 close 不报错）。
        """
        sid = params.get("sessionId") or params.get("session_id", "")
        if not sid:
            raise _ACPRPCError(_RPC_INVALID_PARAMS, "session/close 缺少必填参数 sessionId")
        existed = self._sessions.pop(sid, None) is not None
        # 无论是否登记过都回收：会话私有 MCP 子进程是真实资源，泄漏比多杀一次更糟
        self._drop_session_mcp(sid)
        if self._session_id == sid:
            # 回落到最后一条仍登记的会话，避免后续无 id 的通知失去目标
            self._session_id = next(reversed(self._sessions), "") if self._sessions else ""
        self._log(f"关闭会话: {sid}（{'已释放' if existed else '未登记，幂等清理'}）")
        return {}

    async def _handle_session_delete(self, params: dict[str, Any]) -> dict[str, Any]:
        """ACP v1 删除会话：**连磁盘历史一起删**（与 close 的分界线）。

        - close = 释放本连接侧状态 + 回收会话私有 MCP，历史还在，可再 load
        - delete = 历史文件消失（不可恢复）；同时也要把本连接侧状态与 MCP 清掉，
          否则「删掉的会话」还在吃内存和子进程

        拒绝删除**当前打开**的会话：core 仍持有它，删完下一次保存会把文件写回来
        （用户看到 thread「复活」，这比报错更让人困惑）。请客户端先切走。
        """
        raw = params.get("sessionId") or params.get("session_id", "")
        if not raw or not isinstance(raw, str):
            raise _ACPRPCError(_RPC_INVALID_PARAMS, "session/delete 缺少必填参数 sessionId")
        sid = raw
        if sid == portal.run.status.session_id:
            raise _ACPRPCError(
                _RPC_INVALID_REQUEST,
                f"会话 {sid} 正在本端打开，无法删除（core 仍持有它会把它重新落盘）；请先切换到其他会话",
            )
        existed = self._sessions.pop(sid, None) is not None
        # 无论是否登记过都回收：MCP 子进程是真实资源，泄漏比多杀一次更糟
        self._drop_session_mcp(sid)
        if self._session_id == sid:
            self._session_id = next(reversed(self._sessions), "") if self._sessions else ""
        try:
            deleted = portal.ctl.sessions.delete(sid)
        except Exception as e:
            raise _ACPRPCError(_RPC_INTERNAL_ERROR, f"删除会话失败: {type(e).__name__}: {e}") from e
        disk_note = "已删除" if deleted else "本就不存在"
        link_note = "已释放" if existed else "未登记"
        self._log(f"删除会话: {sid}（磁盘历史{disk_note}，连接侧{link_note}）")
        return {}

    async def _handle_session_set_config_option(self, params: dict[str, Any]) -> dict[str, Any]:
        """ACP v1 切换配置项 —— 本端只有 ``configId="model"``（fp 无 mode/thought_level）。

        顺序是「先验后改」：未知 configId / 未知 value 一律 -32602，绝不猜一个
        最像的选项去执行 —— 静默选错模型会污染用户的钱包，比报错严重得多。

        切换本身**委托命令层**（`/model <provider/model>`）：模型热切换的全部语义
        （就地换 client、同步 `agent.client`、写 ACTIVE_LLM、同步 config 模块内存态）
        只存在于那一个文件里。ACP 自己再实现一遍就是两份会漂移的真相。

        不抢 `_turn_lock`：模型是进程级单例，别处正在跑的轮次已持有旧 client 对象，
        就地换芯对它无害；而一旦抢锁，用户在长轮次期间点下拉框会一直转圈。

        响应 MUST 回**完整** configOptions（schema：part of the response），
        并另发 `config_option_update` 给所有已登记会话 —— 别的 thread 的模型
        下拉框不跟着更新就会显示旧值（同进程只有一个激活模型）。
        """
        self._resolve_session(params, "session/set_config_option")  # 会话必须存在（地址有效性）
        config_id = params.get("configId") or params.get("config_id")
        if config_id != _CONFIG_MODEL_ID:
            raise _ACPRPCError(
                _RPC_INVALID_PARAMS,
                f"不支持的 configId: {config_id!r}（本端只提供 {_CONFIG_MODEL_ID}）",
            )
        value = params.get("value")
        if not isinstance(value, str) or not value:
            # select 变体的 value 是 flatten 的字符串（SessionConfigOptionValue::ValueId）；
            # 其它形态（如 {type:boolean,...}）不是本端提供的选项，明确拒绝而不是强转。
            raise _ACPRPCError(_RPC_INVALID_PARAMS, "value 必须是选项 id 字符串（形如 provider/model）")

        catalog = [f"{p}/{m}" for p, m in _model_catalog()]
        current = _current_model_id()
        if value not in catalog and value != current:
            raise _ACPRPCError(
                _RPC_INVALID_PARAMS,
                f"未知模型: {value}（可选: {', '.join(catalog) or '无配置表'}）",
            )
        if value != current:
            # 命令层可能没装（/model 是扩展命令：public/commands/model.py）。
            # 缺它时**明确报错**，而不是让用户以为切换成功了 —— 下拉框点了没反应
            # 是最难查的那类问题。
            if _MODEL_COMMAND not in portal.run.commands:
                raise _ACPRPCError(
                    _RPC_INTERNAL_ERROR,
                    f"本端未安装 /{_MODEL_COMMAND} 命令，无法切换模型（扩展缺失）",
                )
            _handled, output = await portal.run.command(f"/{_MODEL_COMMAND} {value}")
            # 用**可观测状态**验收，而不是解析命令输出的文案（文案会随扩展漂移）：
            # 真源是 config 模块的激活 id，命令成功时必然等于目标值。
            if _current_model_id() != value:
                raise _ACPRPCError(
                    _RPC_INTERNAL_ERROR,
                    f"切换模型失败（当前 {_current_model_id() or '未知'}）: {output.strip()[:300]}",
                )
            self._log(f"模型已切换: {value}（via /{_MODEL_COMMAND}）")

        options = self._config_options_now()
        # 广播：模型是进程级单例，所有已登记会话的下拉框都要跟着走。
        # 顺序按 sid 排序只为测试可复现（客户端不关心顺序）。
        for sid in sorted(set(self._sessions) | ({self._session_id} if self._session_id else set())):
            self._send_notification(
                "session/update",
                {"sessionId": sid, "update": {"sessionUpdate": "config_option_update", "configOptions": options}},
            )
        return {"configOptions": options}

    def _config_options_now(self) -> list[dict[str, Any]]:
        """当前 configOptions 全量（模型是进程级单例，不按会话存）。"""
        return _config_options(_model_catalog(), _current_model_id())

    async def _handle_session_new(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ACP v1 创建新会话。

        创建后立即通过 notification 注册所有斜杠命令，
        否则 Zed 会拦截所有未注册的 / 命令。

        参考: https://agentclientprotocol.com/protocol/v1/session-setup
        """
        # 规范 MUST：cwd 是会话根目录，决定相对路径解析基准
        cwd = self._validate_session_cwd(params.get("cwd"), "session/new")

        # 只登记，**不碰 core**：客户端常在 A 还在输出时开新 thread，
        # 此刻切 core 会把 A 的对话对象重置（A 的回答落进 B 的历史）。
        # 真正「进入」推迟到该会话的首次 prompt，由 _turn_lock 串行保证。
        sid = portal.ctl.sessions.generate_sid()
        self._sessions[sid] = _SessionState(sid=sid, cwd=cwd)
        self._session_id = sid
        # 客户端转发的 MCP server 登记为**本会话私有**（方案 B）
        self._register_session_mcp(sid, params)
        self._evict_sessions()
        self._log(f"创建新会话: {sid}（cwd={cwd}，首次 prompt 时进入）")

        # 注意：命令注册通知在 _dispatch 中响应之后发送，
        # 以确保 IDE 先拿到 sessionId 再处理命令列表。
        return self._session_setup_result(sid)

    def _session_setup_result(self, sid: str) -> dict[str, Any]:
        """session/new | load | resume 的响应体：sessionId + 当前 configOptions。

        configOptions 必须**建立会话时就带**（稳定面字段）：Zed 用它在会话头部
        渲染模型下拉框，缺了它用户在 IDE 里根本看不到「有模型可选」。
        没有配置表（无 LLM_PROVIDERS / 配置损坏）时整字段省略 —— 给空数组
        在客户端语义是「有配置但没有选项」，比不报更糟。
        """
        payload: dict[str, Any] = {"sessionId": sid}
        options = self._config_options_now()
        if options:
            payload["configOptions"] = options
        return payload

    def _attach_existing_session(self, params: dict[str, Any], method: str) -> tuple[_SessionState, bool]:
        """校验「这条会话真的存在」并纳入会话表（load 与 resume 共用的唯一入口）。

        Returns:
            (会话状态, 磁盘上是否有历史)

        **只登记，不切 core**（load/resume 之后这条会话只是「可寻址」）：
        真正进入要等它的下一次 prompt，否则 load A 会把正在跑的 B 顶掉。

        存在性证明只有三条，客户端声明的 cwd **不能**算：
          ① 本次连接登记过（session/new 的惰性会话）
          ② 磁盘上有历史
          ③ 它就是 core 当前会话（reload/内存态）
        否则 session/load 会退化成「静默新建空会话」——IDE 以为历史已恢复，
        用户打开的是空白对话（这是旧实现的真实 bug）。
        """
        raw = params.get("sessionId") or params.get("session_id", "")
        if not raw or not isinstance(raw, str):
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"{method} 缺少必填参数 sessionId")
        session_id = raw
        cwd = self._validate_session_cwd(params.get("cwd"), method)

        # 盘上文件存在 ⇔ read_messages 非 None（惰性创建：新会话还没产生消息）
        on_disk = portal.ctl.sessions.read_messages(session_id) is not None

        if session_id not in self._sessions and not on_disk and session_id != portal.run.status.session_id:
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"会话不存在: {session_id}")

        st = self._adopt_session(session_id, cwd=cwd)
        if st is None:
            raise _ACPRPCError(_RPC_INVALID_PARAMS, f"会话不存在: {session_id}")

        self._session_id = st.sid
        # 客户端转发的 MCP server 登记为**本会话私有**（方案 B）
        self._register_session_mcp(st.sid, params)

        # 会话 meta 的 cwd 回填/校正：本次 load/resume 时客户端声明的会话根是
        # **唯一可信来源**——老会话（P0 之前创建）meta 里没有 cwd，而 session/list
        # 要按它过滤；项目被移动时也应以客户端当下的声明为准。
        # touch_updated=False：补全字段不该让它在会话列表里跳到最前面。
        # 文件不存在时**不补**：不为了写一个 cwd 字段凭空造出 0 长度会话文件。
        if on_disk:
            try:
                known_cwd = (portal.ctl.sessions.list().get(st.sid) or {}).get("cwd")
                if _norm_cwd(str(known_cwd or "")) != _norm_cwd(st.cwd):
                    portal.ctl.sessions.update_meta(st.sid, create=False, touch_updated=False, cwd=st.cwd)
                    self._log(f"会话 cwd 回填: {st.sid} → {st.cwd}（原 {known_cwd or '未知'}）")
            except Exception as e:  # 回填失败不影响恢复语义
                self._log(f"会话 cwd 回填失败（忽略）: {e}")
        return st, on_disk

    async def _handle_session_load(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ACP v1 恢复已有会话（**响应前回放全部历史**，规范 MUST）。

        参考: https://agentclientprotocol.com/protocol/v1/session-setup#loading-sessions
        """
        st, on_disk = self._attach_existing_session(params, "session/load")
        self._log(f"恢复会话: {st.sid}（cwd={st.cwd}，{'历史回放' if on_disk else '空会话'}）")

        # 规范 MUST：响应之前回放全部历史（见 _replay_history）
        self._replay_history(st.sid)
        # 注意：命令注册通知在 _dispatch 中响应之后发送
        return self._session_setup_result(st.sid)

    async def _handle_session_resume(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ACP v1 挂上已有会话 —— 与 session/load 的唯一差别是**不回放历史**。

        schema 原文：``Resumes an existing session without returning previous
        messages (unlike session/load).``

        为什么值得单开一条路：大历史的会话 `session/load` 要把整段对话重发一遍，
        客户端切 thread 会明显卡顿；resume 只建立寻址关系，客户端自己已有渲染
        缓存（Zed 切回已打开的 thread 就是这种场景）。

        存在性校验与 load 完全一致（见 _attach_existing_session）——「快」不能
        以「不校验」为代价，否则 resume 又会变回静默新建空会话。
        """
        st, on_disk = self._attach_existing_session(params, "session/resume")
        self._log(f"挂上会话: {st.sid}（cwd={st.cwd}，{'有历史，未回放' if on_disk else '空会话'}）")
        # 注意：命令注册通知在 _dispatch 中响应之后发送
        return self._session_setup_result(st.sid)

    async def _handle_session_list(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        ACP v1 列出历史会话。

        参考: https://agentclientprotocol.com/protocol/v1/session-list

        schema（zListSessionsRequest/zListSessionsResponse/zSessionInfo）：
          - 请求**四个字段全可选**。此前把 cwd 当必填并抛 -32602，导致客户端
            按 schema 发 `{}` 时直接失败（该扩展只在知道 cwd 时才带参数）。
          - 请求 cwd 若给出，按**会话自己的** cwd 过滤（realpath 归一）。
            此前把当前会话的 cwd 回填给所有条目，等于谎报：别的项目的会话
            也会出现在本项目的历史列表里。
          - `cursor` 不透明游标 + `nextCursor` 分页（_LIST_PAGE_SIZE 一页）。
          - `updatedAt` 用 ISO 8601（meta 里存的是 `%Y-%m-%d %H:%M:%S`）。

        老会话（本版之前创建、meta 无 cwd）无法证明归属哪个目录：给 cwd 传
        参数时它们不出现；不传时出现但 `cwd` 为空、`_meta.cwdUnknown=true`
        —— 宁可缺字段，不拿当前目录冒充它的工作目录。
        """
        req_cwd = params.get("cwd")
        want = _norm_cwd(req_cwd) if isinstance(req_cwd, str) and req_cwd else None
        cursor = params.get("cursor")
        after = _decode_cursor(cursor) if isinstance(cursor, str) and cursor else None

        all_sessions = portal.ctl.sessions.list()
        current_sid = portal.run.status.session_id

        # ① 归一成 (updated, sid, meta, 会话自己的 cwd)
        rows: list[tuple[str, str, dict[str, Any], str]] = []
        for sid, meta in all_sessions.items():
            updated = str(meta.get("updated") or meta.get("created") or "")
            meta_cwd = meta.get("cwd")
            rows.append((updated, sid, meta, meta_cwd if isinstance(meta_cwd, str) else ""))

        # 排序键必须与游标键完全一致（updated desc，sid 作稳定次序），
        # 否则翻页会跳过或重复条目。
        rows.sort(key=lambda r: (r[0], r[1]), reverse=True)

        # ② 按会话自己的 cwd 过滤（不拿当前会话目录冒充别人的）
        if want is not None:
            rows = [r for r in rows if r[3] and _norm_cwd(r[3]) == want]

        # ③ 游标：只保留严格排在游标之后的条目
        if after is not None:
            rows = [r for r in rows if (r[0], r[1]) < after]

        # ④ 取一页
        page = rows[:_LIST_PAGE_SIZE]
        sessions_list: list[dict[str, Any]] = []
        for updated, sid, meta, meta_cwd_s in page:
            title = meta.get("summary", "") or ""
            sessions_list.append({
                "sessionId": sid,
                "cwd": meta_cwd_s,
                "title": title[:80] if title else None,
                "updatedAt": _to_iso8601(updated),
                "_meta": {
                    "messageCount": meta.get("message_count", 0),
                    "isCurrent": sid == current_sid,
                    "cwdUnknown": not meta_cwd_s,
                },
            })

        result: dict[str, Any] = {"sessions": sessions_list}
        # 还有剩余 → 发下一页游标；不带 nextCursor 表示到底
        if len(rows) > len(page) and page:
            result["nextCursor"] = _encode_cursor(page[-1][0], page[-1][1])
        return result

    async def _handle_session_prompt(self, params: dict[str, Any]) -> dict[str, Any]:
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

        # ── 按 sessionId 路由（协议里 sessionId 是地址，不是「当前会话指针」）──
        st = self._resolve_session(params, "session/prompt")
        sid = st.sid

        # ── 同会话并发：客户端 bug，回请求错误 ──
        # stopReason="cancelled" 只能由 session/cancel 产生；并发冲突回 cancelled
        # 会让客户端误判「用户取消了本次回答」。
        # 注意这里只管**同一会话**：跨会话并发是合法的，走 _turn_lock 排队。
        if st.busy:
            self._log(f"⚠️  会话 {sid} 已有 prompt 在处理，拒绝并发")
            raise _ACPRPCError(_RPC_INVALID_REQUEST, "该会话已有 prompt 正在处理中，请先 session/cancel 或等待完成")

        st.busy = True
        # 尽早挂上 task：排队期间也要能被 session/cancel 定位并取消
        st.task = asyncio.current_task()
        self._session_id = sid
        try:
            # ── 跨会话排队：core 只有一个执行槽/一套上下文 ──
            # 排队而非拒绝 —— 客户端并发发 prompt 是合规的，先到先跑，
            # 后来者只看到「还在等」（规范没有必须并发的 MUST）。
            async with self._turn_lock:
                self._active = st
                cwd_token: Token[str | None] | None = None
                try:
                    cwd_token = self._enter_session(st)
                    return await self._run_turn(st, user_prompt)
                finally:
                    # 复位必须在同一 Task 内（_run_turn 是 await，未跨 Task 边界）；
                    # 失败也要复位，否则本 Task 残留 cwd 会污染后续同 Task 的代码。
                    if cwd_token is not None:
                        reset_cwd(cwd_token)
                    self._active = None
        finally:
            st.busy = False
            st.task = None

    async def _run_turn(self, st: _SessionState, user_prompt: str) -> dict[str, Any]:
        """跑一轮对话（调用方已持有 _turn_lock 且已进入该会话）。

        拆出来的理由：`_handle_session_prompt` 负责协议层（路由/排队/取消登记），
        这里负责「把一轮跑完并把输出推给客户端」，两件事的失败模式完全不同。
        """
        sid = st.sid
        # 标题快照：本轮结束时用它判断 session_info_update 该不该发。
        # 必须在 turn 之前取 —— 第一轮问答后 core 才派生标题，此时 before 是空串，
        # 于是「新会话首轮」也能正常推送；若靠 _title_before 初值猜则首轮会漏。
        self._title_before[sid] = self._session_title(sid)
        # 用量快照同理：清掉上一轮的残留值，本轮没调 LLM（纯命令）时就不报用量
        # （宁可这条通知缺席，也不要拿上一轮的数字冒充本轮）。
        self._usage_used.pop(sid, None)

        # ── 发送 plan 通知 ──
        self._send_plan_notification(session_id=sid)

        # ── 调用 Agent 核心 ──
        # 使用 ACPIO 缓冲输出，并在累积到阈值时自动推送 agent_message_chunk
        #
        # 注意：send_chunk 回调捕获了 sid 快照，避免 race；
        # 客户端声明 elicitation.form 时同时注入带内 elicit。
        acp_io = self._make_acp_io(sid)

        self._log(f"发送给 Agent: {user_prompt[:120]}")
        turn = asyncio.create_task(portal.run.send(user_prompt, io=acp_io))
        try:
            response = await turn
        except asyncio.CancelledError:
            self._log("⏹️  Agent 处理已被用户取消")
            # 清理中断标记，确保下次 prompt 正常工作
            portal.run.reset_cancelled()
            # 取消后仍然 flush 缓冲区，确保用户看到已生成的内容
            buf = acp_io.flush_text()
            if buf.strip():
                self._send_message_notification(buf, session_id=sid)

            self._clear_plan(sid)
            self._notify_session_info(sid)
            # 取消也要报用量：用户中断时上下文已经涨了，客户端显示的占用得跟上
            self._usage_update(self._usage_used.pop(sid, 0), session_id=sid)
            return {"stopReason": "cancelled"}
        finally:
            if not turn.done():
                turn.cancel()

        # ── 处理剩余文本（流式推送后缓冲区可能已空） ──
        # 设计原则：io 负责展示，return 负责状态传递
        # - IO 通道有内容（命令场景）→ 由 IO 负责展示，不重复推 response.content
        # - IO 通道无内容（对话/工具场景）→ fallback 到 response.content
        buf = acp_io.flush_text()
        reply_text = response.content or ""

        if buf.strip():
            self._send_message_notification(buf, session_id=sid)
        elif reply_text:
            self._send_message_notification(reply_text, session_id=sid)

        # plan 条目必须走完生命周期，不能只发开始态
        self._clear_plan(sid)
        self._notify_session_info(sid)
        # 用量放在最后：客户端此时已收完全部 chunk，进度条/占用数字不会与正文抢渲染
        self._usage_update(self._usage_used.pop(sid, 0), session_id=sid)
        return {"stopReason": "end_turn"}

    async def _handle_session_ping(self, params: dict[str, Any]) -> dict[str, Any]:
        """ACP v1 心跳保持"""
        return {"pong": True}

    async def _handle_session_commands(self, params: dict[str, Any]) -> dict[str, Any]:
        """
        返回可用斜杠命令列表（作为 session/commands 请求的 fallback）。

        标准 ACP v1 通过 available_commands_update 通知注册命令，
        此 handler 作为补充，覆盖 IDE 通过请求方式查询命令的场景。

        命令名不带 '/' 前缀（ACP v1 约定），IDE 端会自行添加。
        """
        try:
            cmds = portal.run.commands
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

    def _extract_prompt(self, params: dict[str, Any]) -> str | None:
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
        prompt_list = cast("list[dict[str, Any]] | None", params.get("prompt"))
        if prompt_list is not None and isinstance(prompt_list, list) and len(prompt_list) > 0:  # type: ignore[reportUnnecessaryIsInstance]
            return self._prompt_blocks_to_text(prompt_list)

        # ── 方式2: OpenAI 兼容 messages ──
        messages = cast("list[dict[str, Any]] | None", params.get("messages"))
        if messages is not None and isinstance(messages, list) and len(messages) > 0:  # type: ignore[reportUnnecessaryIsInstance]
            last_msg = messages[-1]
            if last_msg.get("role") in ("user", "assistant"):
                return self._build_messages_text(messages)

        return None

    def _prompt_blocks_to_text(self, blocks: list[dict[str, Any]]) -> str:
        """
        将 ACP v1 的 content blocks 转换为文本。

        支持:
          - text block: {"type": "text", "text": "..."}
          - resource block: {"type": "resource", "resource": {"uri": "...", "text": "...", ...}}

        组合规则：
          - 所有 text blocks 按顺序拼接
          - resource blocks 转换为 [文件: uri]\n内容 格式
        """
        parts: list[str] = []
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

            elif block_type == "resource_link":
                # 规范 baseline MUST：所有 agent 必须支持 text 与 resource_link。
                # 此前该类型被静默丢弃 → IDE 里 @文件 引用在 prompt 中凭空消失。
                # resource_link 只给指针不给内容，需提示 agent 自行读取。
                uri = block.get("uri", "")
                name = block.get("name") or uri
                mime = block.get("mimeType", "")
                detail = f"[文件引用: {name} → {uri}"
                if mime:
                    detail += f" ({mime})"
                detail += "]（仅引用，未附带内容；如需内容请用 read_file 工具读取该路径）"
                parts.append(detail)

        return "\n".join(parts)

    def _build_messages_text(self, messages: list[dict[str, Any]]) -> str:
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
            cmds = portal.run.commands
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

    _PLAN_STATUSES = ("pending", "in_progress", "completed")

    def _send_plan_notification(self, session_id: str | None = None, *, status: str = "in_progress") -> None:
        """发送 ACP v1 plan 更新通知

        PlanEntryStatus 枚举只有 pending/in_progress/completed（旧实现的 "running" 非法，
        会让客户端 schema 校验失败）。计划条目必须走完生命周期，不能只发开始态。
        """
        sid = session_id or self._turn_sid()
        if status not in self._PLAN_STATUSES:
            status = "in_progress"
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "plan",
                    "entries": [
                        {
                            "content": "FP 正在处理请求...",
                            "priority": "high",
                            "status": status,
                        },
                    ],
                },
            },
        )

    def _clear_plan(self, session_id: str | None = None) -> None:
        """清空计划（取消/失败路径），避免计划永久停在 in_progress。"""
        self._send_notification(
            "session/update",
            {
                "sessionId": session_id or self._turn_sid(),
                "update": {"sessionUpdate": "plan", "entries": []},
            },
        )

    def _send_message_notification(
        self, text: str, session_id: str | None = None, message_id: str | None = None
    ) -> None:
        """通过 agent_message_chunk 通知发送回复内容

        messageId 语义（规范）：同一条消息的所有 chunk 共享同一 id，id 变化 = 新消息开始。
        故由 _make_acp_io 为每个用户回合生成一个 id 并全程复用 —— 旧实现每 chunk 新建
        uuid，会把一条回复在客户端切成多条独立消息。
        """
        sid = session_id or self._turn_sid()
        mid = message_id or f"msg_{uuid.uuid4().hex[:12]}"
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_message_chunk",
                    "messageId": mid,
                    "content": {
                        "type": "text",
                        "text": text,
                    },
                },
            },
        )

    def _send_thought_notification(
        self, text: str, session_id: str | None = None, message_id: str | None = None
    ) -> None:
        """通过 agent_thought_chunk 推送一段思考（ACP v1 ContentChunk 形状）。

        与 agent_message_chunk 的区别只在 sessionUpdate 取值：客户端据此把内容
        渲染进「思考」区域而不是正文。缺了这条通道，模型的推理过程在 IDE 里
        完全不可见（WebUI 侧一直有 thinking_content，ACP 侧此前一直丢弃）。
        """
        sid = session_id or self._turn_sid()
        mid = message_id or f"tht_{uuid.uuid4().hex[:12]}"
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {
                    "sessionUpdate": "agent_thought_chunk",
                    "messageId": mid,
                    "content": {"type": "text", "text": text},
                },
            },
        )

    def _send_notice(self, severity: str, title: str, description: str | None = None) -> None:
        """推送 notice（结构化告警，客户端可显示在通知区而不是正文流）。

        门控：客户端未声明 `clientCapabilities.session.notices` 时 **MUST NOT** 发
        notice。未声明时降级为 agent_message_chunk —— 规范允许（"MAY 用普通输出
        代替"），且比静默丢弃好：用户至少能看到「MCP server 没起来」。

        severity 取值：info / warning / error（schema 另有 `_` 前缀的实现扩展位，
        本端不用 —— 客户端认不出的值只会被降级成普通文字）。
        """
        params = {"severity": severity, "title": title}
        if description:
            params["description"] = description
        sid = self._turn_sid()
        if self._client_notices:
            self._send_notification(
                "session/update",
                {"sessionId": sid, "update": {"sessionUpdate": "notice", **params}},
            )
            return
        icon = {"info": "ℹ️", "warning": "⚠️", "error": "❌"}.get(severity, "ℹ️")
        text = f"{icon} {title}" + (f"：{description}" if description else "")
        self._send_message_notification(text, session_id=sid)

    def _usage_update(self, prompt_tokens: int, session_id: str | None = None) -> None:
        """推送 usage_update：`used` = 本轮上下文占用，`size` = fp 的上下文预算。

        `used` 取最近一次 LLM 调用的 prompt_tokens —— 它是**上下文实际长度**的
        真实测量值（含 system prompt/历史/工具结果），比本地估算准。
        `size` 用 fp 自己的压缩阈值（见 _context_window_tokens）：不是模型真实窗口，
        但才是「用到多少会触发压缩」的分母。
        cost 不发：fp 没有价格表，编一个数比不发更糟（schema 里 cost 可省略）。

        prompt_tokens<=0（本轮没调过 LLM / 后端没回 usage）时**不发**：
        used=0 在客户端是「上下文空着」，是假话。
        """
        sid = session_id or self._turn_sid()
        if not sid or prompt_tokens <= 0:
            return
        size = max(prompt_tokens, _context_window_tokens())
        self._send_notification(
            "session/update",
            {
                "sessionId": sid,
                "update": {"sessionUpdate": "usage_update", "used": prompt_tokens, "size": size},
            },
        )

    def _make_acp_io(self, sid: str | None) -> ACPIO:
        """构造 per-prompt ACPIO；客户端声明 elicitation.form 时注入带内 elicit 回调。

        整轮共用一个 messageId（见 _send_message_notification 的语义说明）；
        思考流另外自持 messageId（见 ACPIO.flush_thought）。
        """
        message_id = f"msg_{uuid.uuid4().hex[:12]}"

        def send(text: str) -> None:
            self._send_message_notification(text, session_id=sid, message_id=message_id)

        def send_thought(text: str, thought_id: str) -> None:
            self._send_thought_notification(text, session_id=sid, message_id=thought_id)

        if not self._client_elicitation.get("form"):
            return ACPIO(send_chunk=send, send_thought=send_thought)

        async def _elicit(prompt: str, options: list[str], suggest: str) -> dict[str, Any]:
            return await self._elicit_form(sid, prompt, options, suggest)

        return ACPIO(send_chunk=send, elicit=_elicit, send_thought=send_thought)

    async def _shutdown_agent(self) -> None:
        """安全关闭实例（DISCARD：不留痕，删除当前会话）"""
        try:
            with contextlib.redirect_stdout(sys.stderr):
                await portal.ctl.close(reason=ExitReason.DISCARD)
        except Exception as e:
            self._log(f"关闭实例时出错: {e}")

    # ═══════════════════════════════════════════════════════
    # JSON-RPC 通信（写真正的 stdout）
    # ═══════════════════════════════════════════════════════

    def _send_result(self, req_id: Any, result: dict[str, Any] | None) -> None:
        """发送 JSON-RPC 成功响应"""
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": req_id, "result": result},
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()

    def _send_error(self, req_id: Any, code: int, message: str) -> None:
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

    def _send_notification(self, method: str, params: dict[str, Any]) -> None:
        """发送 JSON-RPC 通知（无 id）"""
        msg = json.dumps(
            {"jsonrpc": "2.0", "method": method, "params": params},
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()

    # ── agent→client 请求（elicitation 等）──

    def _send_request(self, method: str, params: dict[str, Any]) -> tuple[int, asyncio.Future[dict[str, Any]]]:
        """发一条 JSON-RPC 请求给客户端（agent→client），返回 (id, 等待响应的 Future)。"""
        self._req_counter += 1
        rid = self._req_counter
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        self._pending_requests[rid] = fut
        msg = json.dumps(
            {"jsonrpc": "2.0", "id": rid, "method": method, "params": params},
            ensure_ascii=False,
        )
        self._stdout.write(msg + "\n")
        self._stdout.flush()
        return rid, fut

    def _resolve_pending(self, response: dict[str, Any]) -> None:
        """收到 agent→client 请求的响应：唤醒对应 Future（result 或 error）。"""
        rid = response.get("id")
        fut = self._pending_requests.pop(rid, None) if isinstance(rid, int) else None
        if fut is None or fut.done():
            return
        if "error" in response:
            fut.set_exception(RuntimeError(f"client error: {response.get('error')}"))
        else:
            result = response.get("result")
            fut.set_result(cast("dict[str, Any]", result) if isinstance(result, dict) else {})

    async def _elicit_form(
        self, session_id: str | None, prompt: str, options: list[str], suggest: str
    ) -> dict[str, Any]:
        """向客户端发起标准 elicitation/create（form mode），等待带内响应。

        返回响应 result（形如 {"action": "accept"/"decline"/"cancel", "content": {...}}）；
        异常/拒绝的语义处理由调用方（ACPIO.ask）负责回退。全程标准协议，无客户端特化。
        """
        schema = _build_form_schema(options, suggest)
        rid, fut = self._send_request(
            "elicitation/create",
            {
                "sessionId": session_id or self._turn_sid(),
                "mode": "form",
                "message": prompt,
                "requestedSchema": schema,
            },
        )
        try:
            return await fut
        finally:
            # 超时/取消时清理未决（响应已到则 _resolve_pending 已 pop）
            self._pending_requests.pop(rid, None)

    @staticmethod
    def _log(msg: str) -> None:
        """日志输出到 stderr"""
        print(f"[ACP] {msg}", file=sys.stderr, flush=True)


# ═══════════════════════════════════════════════════════════
# 入口
# ═══════════════════════════════════════════════════════════


def main() -> None:
    """启动 ACP Server 的入口函数"""
    # ── 捕获原始启动命令（reload 激活核心 execve 重启用，新入口契约第 0 步）──
    portal.ctl.bootstrap()

    server = ACPServer()
    try:
        asyncio.run(server.start())
    except KeyboardInterrupt:
        print("[ACP] 用户中断", file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
