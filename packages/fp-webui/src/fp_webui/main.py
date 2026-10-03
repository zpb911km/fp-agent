"""
FP — WebUI 服务器
================================

插件模式的 Web 用户界面，不修改 core/ 中的任何代码。

架构：
  ┌─────────────┐   生命周期钩子    ┌───────────┐   WebSocket   ┌──────────┐
  │  Agent 核心  │ ──────────────→ │  EventBus  │ ────────────→ │  前端 UI  │
  │             │                  │ (pub/sub)  │               │ (浏览器)  │
  └─────────────┘                  └───────────┘               └──────────┘

用法：
  cd /path/to/fp-agent
  python3 -m app.webui.main

  或：
  python3 app/webui/main.py

  然后打开浏览器访问 http://localhost:8765
"""

import argparse
import asyncio
import json
import logging
import os
import secrets
import socket
import sys
import time
from collections.abc import Awaitable, Callable
from contextlib import asynccontextmanager, suppress
from typing import Any, cast
from urllib.parse import parse_qsl, urlencode, urlsplit

from fp_core.platform_utils import get_data_dir

# ── FastAPI / WebSocket ─────────────────────────────────
try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request, Response, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as e:
    print(f"[WebUI] 缺少依赖: {e}")
    print("  → 请安装: pip install -r app/webui/requirements.txt")
    print("  → 或:     pip install .[webui]")
    sys.exit(1)

# ── 唯一接口组导入（协议 §5：前端只许 fp_core.api + 纯工具模块） ──
from fp_core.api import ExitReason, RestIO, WebSocketIO, portal
from fp_core.logger import get_logger

# EventBus 类已下沉至 core（fp_core.api.EventBus），webui 直接用 portal.events
# （transport 层的 seq/run_id 断连续传语义原样保留）。
event_bus = portal.events

# ════════════════════════════════════════════════════════════
# 1a. SessionRuntime — 会话级运行时（与 WS 连接解耦）
# ════════════════════════════════════════════════════════════


class SessionRuntime:
    """
    会话级运行时：agent 处理任务与 IO 通道的归属方。

    L1 改造的核心：任务/IO 原先是 /ws/chat 连接函数的局部变量，
    WebSocket 断开（浏览器关闭/休眠）时 finally 会 cancel 处理任务，
    导致 agent 中途停止。现上提为模块级单例：
      - 断开只清理传输层（push_task + 订阅），不碰处理任务
      - 重连的任意连接可继续 feed ask 回复 / 发 cancel / 收事件
      - 任务的取消只来自：用户显式 cancel、服务 shutdown、任务自身结束
    """

    def __init__(self):
        self.active_task: asyncio.Task[None] | None = None
        self.current_io: WebSocketIO | None = None

    @property
    def is_running(self) -> bool:
        """是否仍有处理任务在运行"""
        return self.active_task is not None and not self.active_task.done()

    def start(self, io: WebSocketIO, coro: Any) -> asyncio.Task[None]:
        """启动处理任务并登记 IO 通道（会话级唯一活跃任务）"""
        self.current_io = io
        self.active_task = asyncio.create_task(coro)
        return self.active_task

    def feed_reply(self, text: str, ask_id: str | None = None) -> bool:
        """把用户回复注入当前等待 ask 的 IO（协议面：经 portal.run.reply 转发）"""
        return portal.run.reply(text, ask_id)

    def cancel_active(self) -> bool:
        """取消当前处理任务；返回是否找到了可取消的任务"""
        if self.is_running and self.active_task is not None:
            self.active_task.cancel()
            return True
        return False

    def release(self) -> None:
        """任务结束后释放登记（由处理任务的 finally 调用）"""
        self.current_io = None

    async def shutdown(self) -> None:
        """服务端关闭：取消活跃任务并等待其收尾（唯一非用户主动的取消点）"""
        if self.active_task is not None and not self.active_task.done():
            self.active_task.cancel()
            with suppress(asyncio.CancelledError):
                await self.active_task
        self.release()
        self.active_task = None

    def snapshot(self) -> dict[str, Any]:
        """供重连 connected 事件携带的运行时状态快照"""
        pending_reply = getattr(self.current_io, "_pending_reply", None)
        pending_ask = pending_reply is not None and not pending_reply.done()
        return {
            "processing": self.is_running,
            "pending_ask": bool(pending_ask),
        }


# 全局会话运行时
session_runtime = SessionRuntime()


# ════════════════════════════════════════════════════════════
# 1b. 认证 — 自生成启动 Token（幂等）
# ════════════════════════════════════════════════════════════

_TOKEN_DIR: str = os.path.join(
    get_data_dir(),
)
_TOKENS_DIR: str = os.path.join(_TOKEN_DIR, "tokens")
os.makedirs(_TOKENS_DIR, exist_ok=True)
_TOKEN_FILE: str = os.path.join(_TOKENS_DIR, ".webui_token")


def _load_or_create_token() -> str:
    """
    读取已有 token 文件，或生成新 token 写入文件。
    幂等设计：无论模块被 import 多少次，都返回同一 token。
    """
    try:
        if os.path.exists(_TOKEN_FILE):
            with open(_TOKEN_FILE) as f:
                stored = f.read().strip()
                if stored and len(stored) >= 32:
                    return stored
    except OSError:
        pass
    # 文件不存在或内容无效 → 生成新 token
    new_token = secrets.token_urlsafe(32)
    try:
        with open(_TOKEN_FILE, "w") as f:
            f.write(new_token)
        os.chmod(_TOKEN_FILE, 0o600)
    except PermissionError:
        pass  # Windows 不支持 chmod，静默跳过
    except OSError:
        pass
    return new_token


_WEBUI_TOKEN: str = _load_or_create_token()


# ════════════════════════════════════════════════════════════
# 3. FastAPI 应用
# ════════════════════════════════════════════════════════════

_agent_lock = asyncio.Lock()


async def ensure_agent() -> None:
    """确保实例已开启（延迟初始化，双检锁）

    实例生命周期归 portal.ctl；webui 不再持有 Agent 引用。
    """
    if portal.is_open:
        return
    async with _agent_lock:
        if portal.is_open:
            return
        await portal.ctl.open(enable_log=False, resume=os.environ.get("FP_RELOAD_SID") or None)
        get_logger().info(f"[WebUI] 实例已初始化 (model={portal.run.status.model})")


async def _run_reload_continuation() -> None:
    """reload 后启动续接（机制契约见 fp_core.core.handoff，webui 入口适配）。

    续接事件全部进 EventBus 环形缓冲；客户端重连时凭 run_id 变化触发
    resync 全量重拉——因此即使续接先于重连完成，结果也不丢。
    并发由 session_runtime 守住：续接运行期间用户消息会被 is_running 拒绝。
    """
    if not os.environ.get("FP_RELOAD_HANDOFF"):
        return
    try:
        await ensure_agent()
        _directive = portal.ctl.take_reload()
        if _directive.notice:
            get_logger().info(f"[WebUI] {_directive.notice}")
            # 复用既有 reload_done 事件（前端 app.js 已渲染"重载完成"状态行）
            _st = portal.run.status
            await event_bus.publish({
                "type": "reload_done",
                "session_id": _st.session_id,
                "model": _st.model,
            })
        if not _directive.should_continue:
            return
    except Exception as e:
        get_logger().error(f"[WebUI] reload handoff 消费失败，跳过续接: {e}")
        os.environ.pop("FP_RELOAD_HANDOFF", None)
        os.environ.pop("FP_RELOAD_SID", None)
        return

    ws_io = WebSocketIO(event_bus)
    ws_io.is_running = True

    async def _run() -> None:
        try:
            response = await portal.run.continue_(io=ws_io)
            _st = portal.run.status
            all_msgs = portal.run.transcript
            non_sys_count = sum(1 for m in all_msgs if m.get("role") != "system")
            await event_bus.publish({
                "type": "done",
                "session_id": _st.session_id,
                "final_content": response.content,
                "non_system_count": non_sys_count,
            })
        except asyncio.CancelledError:
            await event_bus.publish({"type": "cancelled"})
        except Exception as e:
            await event_bus.publish({"type": "error", "error": str(e)})
            await event_bus.publish({"type": "done", "error": str(e)})
        finally:
            ws_io.is_running = False
            if session_runtime.current_io is ws_io:
                session_runtime.release()

    session_runtime.start(ws_io, _run())
    get_logger().info("[WebUI] 🔄 reload 续接任务已启动")


# ── 生命周期管理 ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化 Agent，关闭时清理"""
    get_logger().info("[WebUI] 🚀 FP WebUI 启动中...")

    # ── reload handoff：进程重启后续接上一轮对话（不阻塞启动，
    #    任务进后台，事件由环形缓冲 + run_id 重放/resync 兜底） ──
    await _run_reload_continuation()

    # Agent 延迟初始化，第一次请求时创建
    yield

    # 关闭
    get_logger().info("[WebUI] 🛑 正在关闭...")
    # 服务端决定终止 → 这是 WS 断连之外唯一合法的取消时机
    await session_runtime.shutdown()
    await portal.ctl.close()  # FINAL：保存+摘要+静默收尾（幂等，未开启时 no-op）
    await event_bus.shutdown()
    get_logger().info("[WebUI] ✅ 已关闭")


# ── FastAPI 实例 ─────────────────────────────────────────

app = FastAPI(
    title="FP WebUI",
    description="FP AI Agent 的 Web 界面",
    version="1.0.0",
    lifespan=lifespan,
)


# ── 认证中间件 ──────────────────────────────────────────

_AUTH_WHITELIST = {"/api/auth", "/api/health"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """拦截 /api/* 请求，验证 Bearer Token（白名单除外）"""
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_WHITELIST:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {_WEBUI_TOKEN}"
        if not auth or auth != expected:
            return JSONResponse(status_code=401, content={"detail": "未授权，请先登录"})
    return await call_next(request)


# ── 访问日志（URL 脱敏，防止 token 明文泄露） ────────────

_SENSITIVE_QUERY_KEYS = {"token", "key", "secret", "password", "access_token"}


def _sanitize_url(url: str) -> str:
    """脱敏 URL：将 query 中的敏感参数（token 等）打码，防止日志泄露"""
    parts = urlsplit(url)
    params = parse_qsl(parts.query, keep_blank_values=True)
    masked = [(k, "***" if k.lower() in _SENSITIVE_QUERY_KEYS else v) for k, v in params]
    query = urlencode(masked)
    return parts.path + (f"?{query}" if query else "")


def _get_lan_ip() -> str:
    """获取本机局域网 IP（0.0.0.0 监听时用于展示可访问地址）"""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect 不真正发包，仅让内核选择路由并返回本机 IP
        s.connect(("192.168.255.255", 1))
        ip = s.getsockname()[0]
        if ip and not ip.startswith("127."):
            return ip
    except Exception:
        pass
    finally:
        s.close()
    return "127.0.0.1"


@app.middleware("http")
async def access_log_middleware(request: Request, call_next: Callable[[Request], Awaitable[Response]]) -> Response:
    """记录访问日志，URL 脱敏（不暴露 query 中的 token）"""
    start = time.monotonic()
    method = request.method
    path = _sanitize_url(str(request.url))
    is_ws = request.headers.get("upgrade", "").lower() == "websocket"

    if is_ws:
        # WebSocket 握手：立即记录（避免阻塞到连接关闭）
        get_logger().info(f"[WebUI] {method} {path}（WS 连接）")
        response = await call_next(request)
        return response

    response = await call_next(request)
    duration_ms = (time.monotonic() - start) * 1000
    get_logger().info(f"[WebUI] {method} {path} → {response.status_code}（{duration_ms:.0f}ms）")
    return response


# ════════════════════════════════════════════════════════════
# 4. REST API 端点
# ════════════════════════════════════════════════════════════

# ── 简单限流：每 IP 每 10 秒最多 5 次尝试 ──────────
_AUTH_LIMIT_WINDOW = 10  # 窗口秒数
_AUTH_LIMIT_MAX = 5  # 窗口内最大尝试次数
_auth_attempts: dict[str, list[float]] = {}  # ip → [时间戳列表]


def _check_auth_rate_limit(client_ip: str) -> None:
    """检查客户端认证频率，超限则抛 429"""
    now = time.time()
    window_start = now - _AUTH_LIMIT_WINDOW
    records = _auth_attempts.get(client_ip, [])
    # 清理过期记录
    records = [t for t in records if t > window_start]
    if len(records) >= _AUTH_LIMIT_MAX:
        raise HTTPException(
            status_code=429,
            detail="认证尝试过于频繁，请稍后再试",
        )
    records.append(now)
    _auth_attempts[client_ip] = records


@app.post("/api/auth")
async def auth_login(request: Request, body: dict[str, Any]) -> dict[str, Any]:
    """验证 Token 并登录（每 IP 限频）"""
    client_ip = request.client.host if request.client else request.headers.get("x-forwarded-for", "unknown")
    _check_auth_rate_limit(client_ip)
    token = body.get("token", "").strip()
    if secrets.compare_digest(token, _WEBUI_TOKEN):
        return {"status": "ok", "message": "验证通过"}
    raise HTTPException(status_code=401, detail="Token 无效")


@app.get("/api/health")
async def health_check() -> dict[str, Any]:
    """健康检查端点"""
    await ensure_agent()
    _st = portal.run.status
    return {
        "status": "ok",
        "agent": _st.model,
        "session": _st.session_id,
        "subscribers": event_bus.subscriber_count,
        "processing": session_runtime.is_running,
        "run_id": event_bus.run_id,
    }


@app.post("/api/chat")
async def send_message(body: dict[str, Any]) -> dict[str, Any]:
    """
    发送消息并获取回复（非流式）

    请求体:
      {"message": "你好"}

    返回:
      {"response": "...", "session_id": "..."}
    """
    message = body.get("message", "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="消息不能为空")

    await ensure_agent()

    # 发送消息时启动一个独立的后台任务来处理
    # 前端通过 WebSocket 接收实时更新
    # 使用 RestIO 避免交互式命令（如 /back 无参数）阻塞
    response = await portal.run.send(message, io=RestIO())

    return {
        "response": response.content,
        "session_id": portal.run.status.session_id,
    }


@app.get("/api/sessions")
async def list_sessions() -> dict[str, Any]:
    """列出所有历史会话"""
    await ensure_agent()
    sessions = portal.ctl.sessions.list()
    current_sid = portal.run.status.session_id

    result: list[dict[str, Any]] = []
    for sid, info in sorted(sessions.items(), key=lambda x: x[1].get("created", ""), reverse=True):
        result.append({
            "id": sid,
            "message_count": info.get("message_count", 0),
            "created": info.get("created", ""),
            "summary": info.get("summary", ""),
            "is_current": sid == current_sid,
        })

    return {"sessions": result}


@app.get("/api/commands")
async def list_commands():
    """返回快捷命令列表（从 fp-core 命令注册表动态获取，自动适配新增命令）"""
    cmds = portal.run.commands
    return {"commands": [{"name": f"/{name}", "desc": desc} for name, desc in cmds.items()]}


# ════════════════════════════════════════════════════════════
# 4a. 实例替换核心逻辑（新建 / 重载共享）
# ════════════════════════════════════════════════════════════


async def _replace_agent() -> dict[str, Any]:
    """
    替换全局实例的核心逻辑（/api/agent/new 专用）。

    进程级代码热重启已统一到命令面 `/reload`（fp_core.core.handoff：
    落盘 → execve 原启动命令重启）——本函数只负责"内存状态清空、
    新建实例"，不再做同进程 importlib 原地重载。

    安全保证：
      - 如果实例正在处理请求，返回 409 拒绝
      - 替换期间 portal.is_open 为 False，ensure_agent() 自动创建新实例
      - 活跃的 WebSocket 连接经 portal.run.* 自动指向新实例
    """
    if portal.is_open and portal.run.status.is_processing:
        raise HTTPException(status_code=409, detail="Agent 正在处理请求，请稍后重试")

    async with _agent_lock:
        # ── 落盘旧会话并关闭旧实例（RECYCLE：保存+摘要，静默） ──
        if portal.is_open:
            with suppress(Exception):
                await portal.ctl.close(reason=ExitReason.RECYCLE)

        # ── 通知前端准备重连 ──
        await event_bus.publish({
            "type": "reload",
            "message": "🔄 Agent 正在新建，连接即将断开",
        })

        # ── 创建新实例 ──
        try:
            await portal.ctl.open(enable_log=False)
        except Exception as e:
            get_logger().error(f"[WebUI] ❌ 新实例创建失败: {e}")
            raise HTTPException(status_code=500, detail=f"新 Agent 创建失败: {e}") from e

        # ── 会话管理：新建会话 ──
        # （恢复旧会话分支已随命令面统一删除：会话恢复语义在
        #   fp_core.core.handoff 的 execve 热重启里，由 resume=FP_RELOAD_SID 完成）
        try:
            info = portal.ctl.sessions.new()
            get_logger().info(f"[WebUI] 🆕 已使用新会话: {info.session_id}")
        except Exception as e:
            get_logger().error(f"[WebUI] ❌ 新会话初始化失败: {e}")
            raise HTTPException(status_code=500, detail=f"新会话初始化失败: {e}") from e

        _st = portal.run.status
        get_logger().info(f"[WebUI] 🆕 实例新建完成 (model={_st.model}, session={_st.session_id})")

        # ── 稍等片刻，让前端收到 reload 事件后再推送 done ──
        await asyncio.sleep(0.3)
        await event_bus.publish({
            "type": "reload_done",
            "session_id": _st.session_id,
            "model": _st.model,
        })

    _st = portal.run.status
    return {
        "status": "ok",
        "session_id": _st.session_id,
        "model": _st.model,
    }


@app.post("/api/agent/new")
async def new_agent():
    """
    创建全新 Agent 实例（不重载模块、不恢复旧会话）。

    相当于 Agent 刚启动时的状态，所有内存状态被清空。
    """
    return await _replace_agent()


@app.post("/api/sessions")
async def create_new_session():
    """创建新会话并切换到它"""
    await ensure_agent()
    info = portal.ctl.sessions.new()
    return {"session_id": info.session_id, "status": "created"}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """删除指定会话（不能是当前会话）"""
    await ensure_agent()

    # 检查是不是当前会话
    if session_id == portal.run.status.session_id:
        raise HTTPException(status_code=400, detail="不能删除当前正在使用的会话")

    if not portal.ctl.sessions.delete(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或删除失败")

    return {"status": "deleted", "session_id": session_id}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str) -> dict[str, Any]:
    """
    获取指定会话的完整消息列表（直接读文件，不修改 Agent 状态）。

    ⚠️ index = 非 system 消息的 1-based 索引（与 /back 命令的索引体系一致）。
    跳过 role=system 的消息（如 compact 产生的摘要），因为 /back 命令
    使用的是 get_non_system_messages()，两类 system 消息不计入：
      1. 原始 system prompt
      2. compact 产生的摘要 system 消息
      3. repair_tool_ordering 转化的孤儿 tool 消息
    """
    path = portal.ctl.sessions.path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")

    messages: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg: dict[str, Any] = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("__meta__"):
                continue
            messages.append(msg)

    # ── 只对非 system 消息编号（与 ConversationState.back() 的索引规则一致） ──
    # ConversationState.get_non_system_messages() 只返回 role != "system" 的消息，
    # 所以 compact 后产生的 system(摘要) 消息不计入索引。
    # 如果按文件全部消息编号，compact/resume 后前端 data-index 与后端索引会错位。
    result: list[dict[str, Any]] = []
    non_system_idx = 0  # 只对非 system 消息的 1-based 索引
    for msg in messages:
        role = msg.get("role", "")
        entry = {
            "index": None,  # system 消息 index 为 None
            "role": role,
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls"),
            "tool_call_id": msg.get("tool_call_id"),
        }
        if role != "system":
            non_system_idx += 1
            entry["index"] = non_system_idx
        result.append(entry)

    return {
        "session_id": session_id,
        "total": len(result),
        "non_system_count": non_system_idx,
        "messages": result,
    }


# ════════════════════════════════════════════════════════════
# 4a. 文本搜索接口 — 通过内容片段定位消息
# ════════════════════════════════════════════════════════════


@app.post("/api/sessions/{session_id}/search")
async def search_session_messages(session_id: str, body: dict[str, Any]) -> dict[str, Any]:
    """
    通过文本内容片段搜索消息，返回消息的真实文件行号和非 system 索引。

    请求体:
      {"query": "搜索关键词"}            ← 简单文本片段匹配
      {"query": "...", "regex": true}    ← 正则表达式匹配
      {"query": "...", "limit": 10}      ← 最多返回条数（默认 20）

    返回:
      {
        "session_id": "...",
        "total_matches": 3,
        "results": [
          {
            "index": 5,           # 非 system 索引（与 /back 一致）
            "line_number": 7,     # 文件行号（从 1 开始，含 meta 行）
            "role": "assistant",
            "content_preview": "前 200 字符...",
            "tool_calls": [...],
            "tool_call_id": "..."
          }
        ]
      }

    说明：
      - index=null 表示 system 消息，不可用 /back 回溯
      - line_number 可用于文件定位
      - 匹配方式：简单子串匹配（默认）或正则表达式
    """
    path = portal.ctl.sessions.path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")

    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="查询内容不能为空")

    import re

    use_regex = cast(bool, body.get("regex", False))
    limit = min(body.get("limit", 20), 100)

    # ── 编译匹配模式 ──
    pattern: re.Pattern[Any] | None = None
    query_lower: str | None = None
    if use_regex:
        try:
            pattern = re.compile(query)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"正则表达式无效: {e}") from e
    else:
        query_lower = cast(str, query.lower())

    # ── 逐行扫描文件 ──
    results: list[dict[str, Any]] = []
    try:
        with open(path, encoding="utf-8") as f:
            lines = f.readlines()
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"读取会话文件失败: {e}") from e

    non_system_idx = 0
    for line_no, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            msg: dict[str, Any] = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("__meta__"):
            continue

        role = msg.get("role", "")
        content = cast(str, msg.get("content", ""))

        # 计算非 system 索引（与 /back 一致）
        is_system = role == "system"
        if not is_system:
            non_system_idx += 1

        # ── 匹配检测 ──
        matched = (use_regex and pattern is not None and pattern.search(content)) or (
            not use_regex and query_lower is not None and query_lower in content.lower()
        )

        if matched:
            preview = content[:200]
            if len(content) > 200:
                preview += "..."

            results.append({
                "index": non_system_idx if not is_system else None,
                "line_number": line_no,
                "role": role,
                "content_preview": preview,
                "content_length": len(content),
                "tool_calls": msg.get("tool_calls"),
                "tool_call_id": msg.get("tool_call_id"),
                "file_line": line_no,
            })

            if len(results) >= limit:
                break

    return {
        "session_id": session_id,
        "query": query,
        "total_matches": len(results),
        "results": results,
    }


@app.post("/api/sessions/{session_id}/switch")
async def switch_session_endpoint(session_id: str):
    """切换到指定会话"""
    await ensure_agent()
    info = portal.ctl.sessions.load(session_id)
    if info is None:
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    return {
        "session_id": info.session_id,
        "status": "switched",
    }


@app.post("/api/sessions/clear")
async def clear_current_session():
    """清空当前会话"""
    await ensure_agent()
    portal.ctl.sessions.clear()
    return {"status": "cleared"}


# ════════════════════════════════════════════════════════════
# 4b. （已删除）原 /api/reload 同进程 importlib 热重载
#     热重启已统一到命令面 /reload：fp_core.core.handoff
#     （落盘 → handoff → execve 原启动命令重启，入口续接见
#       _run_reload_continuation）；前端「🔄 重载」按钮转发聊天命令。
# ════════════════════════════════════════════════════════════


# ════════════════════════════════════════════════════════════
# 5. WebSocket 端点 — 流式聊天
# ════════════════════════════════════════════════════════════


@app.websocket("/ws/chat")
async def websocket_chat(
    websocket: WebSocket,
    token: str | None = Query(None),
    seq: int = Query(0),
    run_id: str = Query(""),
):
    """
    WebSocket 流式聊天

    连接后，前端可发送 JSON 消息：
      {"type": "message", "content": "你好"}

    服务器通过 WebSocket 推送实时事件：
      {"type": "connected", "run_id", "seq", "session_id",
       "processing", "pending_ask", "replay", "resync", ...}
        ← 连接确认 + 运行时快照 + 重放协商
        ← replay=true 时随后紧跟重放缓冲事件（带 seq）
        ← resync=true 表示缓冲溢出/服务端已重启，前端应 REST 全量重拉
      {"type": "llm_start", "ts": ...}
      {"type": "llm_end", "content": "...", "has_tool_calls": ..., "tool_names": [...]}
      {"type": "tool_call", "name": "...", "args": "..."}
      {"type": "tool_result", "name": "...", "result": "..."}
      {"type": "response", "content": "..."}
      {"type": "ask", "prompt": "选择: "}          ← 命令等待用户输入
      {"type": "info", "content": "..."}          ← IO 通道输出
      {"type": "hint", "content": "..."}
      {"type": "error", "error": "..."}
      {"type": "item", "content": "..."}
      {"type": "done", "session_id": "...", "final_content": "..."}

    断连语义（L1）：连接只是传输层。断开只清理 push_task/订阅，
    agent 处理任务（session_runtime）继续运行；重连后续传。
    """
    await websocket.accept()

    # ── 验证 Token ──
    if not token or not secrets.compare_digest(token, _WEBUI_TOKEN):
        await websocket.send_json({"type": "error", "error": "未授权，请先登录"})
        await websocket.close(code=4001)
        return

    # 记录连接（不打印 token）
    client_ip = websocket.client.host if websocket.client else "unknown"
    get_logger().info(f"[WebUI] WS 连接: {client_ip} → /ws/chat（已认证）")

    # 确保实例已开启（connected 快照需要 session_id）
    await ensure_agent()

    # ── 断连续传协商（L2）──
    # 「快照重放区间」与「订阅」必须落在同一个同步块内：publish 只在 await 点
    # 之间穿插，块内无 await → 重放与队列既无 gap 也无重复。
    #   · replay 列表：seq ≤ 快照点，只从缓冲重放
    #   · 队列事件：seq > 快照点，由 push_task 实时推送
    replay: list[dict[str, Any]] | None = None
    resync = False
    if seq > 0:
        if run_id == event_bus.run_id:
            replay = event_bus.events_since(seq)
            resync = replay is None  # 缓冲溢出（gap）→ 前端降级 REST 全量重拉
        else:
            resync = True  # 服务端已重启，seq 空间不同，无法重放
    will_replay = replay is not None
    sub_id, event_queue = event_bus.subscribe()

    # 客户端读活性时间戳（休眠/静默断链检测，由主接收循环刷新）
    activity: dict[str, float] = {"ts": time.monotonic()}

    # 后台任务跟踪（初始化后供 finally 安全清理）
    push_task: asyncio.Task[None] | None = None

    try:
        # ── 连接确认：传输信息 + 运行时快照 + 重放协商 ──
        await websocket.send_json({
            "type": "connected",
            "sub_id": sub_id,
            "run_id": event_bus.run_id,
            "seq": event_bus.current_seq,
            "session_id": portal.run.status.session_id,
            "replay": will_replay,
            "resync": resync,
            **session_runtime.snapshot(),
        })
        if replay:
            for ev in replay:
                await websocket.send_json(ev)

        # 后台任务：读取 EventBus 并推送至 WebSocket
        async def push_events():
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=30)
                    await websocket.send_json(event)
                except TimeoutError:
                    # 客户端读活性检查：90s 无任何上行（休眠/网络静默断链）
                    # → 主动关闭，逼前端走 onclose 快速重连
                    if time.monotonic() - activity["ts"] > 90:
                        break
                    # 心跳保活
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break
                except Exception:
                    break
            # 推送通道死亡 → 关传输层让主循环退出。
            # 只动连接，不碰 session_runtime（处理任务继续跑）。
            with suppress(Exception):
                await websocket.close()

        push_task = asyncio.create_task(push_events())

        while True:
            raw = await websocket.receive_text()
            activity["ts"] = time.monotonic()
            data = json.loads(raw)

            # 实例热替换由 portal 内部消化：run.* 永远指向当前实例，
            # 旧 WS 连接无需切换引用（reload/new_agent 后无缝续传）。

            if data.get("type") == "message":
                content = data.get("content", "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "error": "消息不能为空"})
                    continue

                # ── 如果 IO 通道正在等待用户输入，直接注入回复 ──
                # （会话级 runtime → 跨连接可注入，断连重连后 ask 不再卡死）
                if session_runtime.feed_reply(content):
                    continue

                # ── 如果处理任务还在运行（非 ask 状态），拒绝 ──
                if session_runtime.is_running:
                    await websocket.send_json({
                        "type": "error",
                        "error": "正在处理中，请等待当前操作完成",
                    })
                    continue

                # ── 正常处理：创建新 IO 通道并启动处理任务 ──
                ws_io = WebSocketIO(event_bus)
                ws_io.is_running = True

                async def process_and_notify(msg: str, io: WebSocketIO):
                    """处理消息并通过 EventBus 推送结果"""
                    try:
                        response = await portal.run.send(msg, io=io)
                        # 检查是否被用户主动中断（工具执行中 task.cancel()）
                        # 打断标记在 core _process_inner 的 except 块中置位，
                        # send() 返回后经 status.cancelled 观测。
                        # 用这种方式而非重新抛出 CancelledError，是为了不破坏
                        # CLI 模式——CLI 的 except CancelledError: break 会退出程序。
                        if portal.run.status.cancelled:
                            portal.run.reset_cancelled()
                            await event_bus.publish({"type": "cancelled"})
                        else:
                            # 从后端获取权威的非 system 消息计数，传递给前端
                            # 前端据此校准 liveMsgIndex，消除前端自增计数器漂移
                            all_msgs = portal.run.transcript
                            non_sys_count = sum(1 for m in all_msgs if m.get("role") != "system")
                            await event_bus.publish({
                                "type": "done",
                                "session_id": portal.run.status.session_id,
                                "final_content": response.content,
                                "non_system_count": non_sys_count,
                            })
                    except asyncio.CancelledError:
                        await event_bus.publish({"type": "cancelled"})
                    except Exception as e:
                        await event_bus.publish({"type": "error", "error": str(e)})
                        await event_bus.publish({"type": "done", "error": str(e)})
                    finally:
                        io.is_running = False
                        # 归还会话级登记（仅当仍是当前任务的 IO，避免误清新任务）
                        if session_runtime.current_io is io:
                            session_runtime.release()

                # 任务归属会话级 runtime，与 WS 连接解耦（L1 核心）
                session_runtime.start(ws_io, process_and_notify(content, ws_io))

            elif data.get("type") == "cancel":
                # 用户请求中断 → 取消会话级处理任务（可由任意连接发起）
                # task.cancel() 注入 CancelledError → agent._process_inner 的
                # tool 执行 except 块捕获 → 标记 _cancelled_by_user = True
                # → process() 正常返回 → process_and_notify 检查标记 → 发布 cancelled。
                # 不重新抛出异常，避免 CLI 的 except CancelledError: break 误退出。
                session_runtime.cancel_active()

            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "error": str(e)})
    finally:
        # ── 只清理传输层 ──（L1：处理任务归属 session_runtime，断连不取消）
        # 浏览器关闭/休眠导致的断开，agent 继续运行；
        # 重连后通过 connected 快照 + seq 重放续传。
        if push_task is not None:
            push_task.cancel()
        event_bus.unsubscribe(sub_id)


# ════════════════════════════════════════════════════════════
# 6. 静态文件服务 + 前端路由
# ════════════════════════════════════════════════════════════

# 挂载静态文件
_static_dir = os.path.join(os.path.dirname(__file__), "static")
os.makedirs(_static_dir, exist_ok=True)

if os.path.isdir(_static_dir):
    app.mount("/static", StaticFiles(directory=_static_dir), name="static")


# ── 主页 ──────────────────────────────────────────────────


@app.get("/")
async def index():
    """返回聊天界面 HTML"""
    index_path = os.path.join(_static_dir, "index.html")
    if os.path.exists(index_path):
        with open(index_path, encoding="utf-8") as f:
            return HTMLResponse(f.read())
    # 如果前端文件不存在，返回说明页面
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>FP WebUI</title></head>
    <body style="background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;
          display:flex;align-items:center;justify-content:center;height:100vh;">
      <div style="text-align:center">
        <h1> FP WebUI</h1>
        <p>API 服务器已启动。</p>
        <p>访问 <a href="/api/health" style="color:#00bcd4">/api/health</a> 检查状态</p>
        <p>前端文件位于: <code>app/static/index.html</code></p>
        <hr style="border-color:#333;width:50%">
        <p style="color:#888">使用 WebSocket 连接: <code>ws://localhost:8765/ws/chat</code></p>
      </div>
    </body>
    </html>
    """)


# ════════════════════════════════════════════════════════════
# 7. 启动入口
# ════════════════════════════════════════════════════════════


class _UvicornBannerFilter(logging.Filter):
    """过滤 uvicorn 自带的启动横幅（会显示监听地址 0.0.0.0，改用自定义横幅）"""

    def filter(self, record: logging.LogRecord) -> bool:
        msg = record.getMessage()
        # 过滤三类噪音：启动横幅（0.0.0.0）、WS 握手日志（token 明文）、
        # websockets 库连接开关（经 uvicorn.error 透传）
        return not (
            "Uvicorn running on" in msg or '"WebSocket ' in msg or msg in ("connection open", "connection closed")
        )


# 定制 uvicorn 日志：保留错误/访问日志，去掉启动横幅
_UVICORN_LOG_CONFIG: dict[str, Any] = {
    "version": 1,
    "disable_existing_loggers": False,
    "filters": {"no_banner": {"()": _UvicornBannerFilter}},
    "formatters": {
        "default": {
            "()": "uvicorn.logging.DefaultFormatter",
            "fmt": "%(levelprefix)s %(message)s",
            "use_colors": None,
        },
        "access": {
            "()": "uvicorn.logging.AccessFormatter",
            "fmt": '%(levelprefix)s %(client_addr)s - "%(request_line)s" %(status_code)s',
        },
    },
    "handlers": {
        "default": {
            "formatter": "default",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
            "filters": ["no_banner"],
        },
        "access": {
            "formatter": "access",
            "class": "logging.StreamHandler",
            "stream": "ext://sys.stderr",
        },
    },
    "loggers": {
        "uvicorn": {"handlers": ["default"], "level": "INFO", "propagate": False},
        "uvicorn.error": {"handlers": ["default"], "level": "INFO", "propagate": False},
        # uvicorn.access 拉高到 WARNING：其访问日志含 token 明文，交自研脱敏中间件
        "uvicorn.access": {"handlers": ["access"], "level": "WARNING", "propagate": False},
        # websockets 库的 "connection open/closed" 噪音
        "websockets.server": {"handlers": ["default"], "level": "WARNING", "propagate": False},
    },
}


def main():
    """启动 WebUI 服务器"""
    # ── 捕获原始启动命令（reload 激活核心 execve 重启用，新入口契约第 0 步）──
    from fp_core.api import portal as _portal

    _portal.ctl.bootstrap()

    parser = argparse.ArgumentParser(description="FP WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发用）")
    parser.add_argument("--expose", action="store_true", help="监听 0.0.0.0，允许局域网设备访问")
    args = parser.parse_args()

    if args.expose:
        args.host = "0.0.0.0"

    # 确定对外展示的地址（0.0.0.0 → 探测局域网 IP，浏览器才能访问）
    display_ip = _get_lan_ip() if args.host == "0.0.0.0" else args.host
    base_url = f"http://{display_ip}:{args.port}/"

    print()
    print("🤖 FP WebUI")
    print()
    if args.host == "0.0.0.0":
        print("  ⚠️  已监听 0.0.0.0，局域网设备可访问此服务")
        print("  ⚠️  请妥善保管 Token，建议使用 HTTPS 反向代理")
        print()
    print(f"  🌐  WebUI: {base_url}")
    print(f"  🔌  WS:    ws://{display_ip}:{args.port}/ws/chat")
    print(f"  📡  API:   {base_url}api/health")
    print()
    # 显示 Token（从文件读，确保与文件一致）
    display_token = _load_or_create_token()
    print(f"  🔑  启动 Token: ...{display_token[-4:]}")
    print(f"  📄  Token 文件: {_TOKEN_FILE}  （cat 查看完整 Token）")
    print()

    uvicorn.run(
        "fp_webui.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        # 注意：不传 log_level！uvicorn 会用它把 uvicorn.error/access
        # 强制覆盖回 INFO，导致 WS 握手日志（含 token 明文）重新出现。
        # 日志级别完全由 _UVICORN_LOG_CONFIG 控制。
        access_log=False,  # 关闭 uvicorn 默认访问日志，改由脱敏中间件记录
        log_config=_UVICORN_LOG_CONFIG,  # 过滤启动横幅 + WS token 明文
    )


if __name__ == "__main__":
    main()
