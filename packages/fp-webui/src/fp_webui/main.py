"""
Five Pebbles — WebUI 服务器
================================

插件模式的 Web 用户界面，不修改 core/ 中的任何代码。

架构：
  ┌─────────────┐   生命周期钩子    ┌───────────┐   WebSocket   ┌──────────┐
  │  Agent 核心  │ ──────────────→ │  EventBus  │ ────────────→ │  前端 UI  │
  │             │                  │ (pub/sub)  │               │ (浏览器)  │
  └─────────────┘                  └───────────┘               └──────────┘

用法：
  cd /media/zpb/data/codes/AI/agent
  python3 -m app.webui.main

  或：
  python3 app/webui/main.py

  然后打开浏览器访问 http://localhost:8765
"""

import argparse
import asyncio
import json
import os
import secrets
import sys
import time
from contextlib import asynccontextmanager, suppress

from fp_core.platform_utils import get_data_dir

# ── FastAPI / WebSocket ─────────────────────────────────
try:
    import uvicorn
    from fastapi import FastAPI, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
except ImportError as e:
    print(f"[WebUI] 缺少依赖: {e}")
    print("  → 请安装: pip install -r app/webui/requirements.txt")
    print("  → 或:     pip install .[webui]")
    sys.exit(1)

# ── Agent 核心导入 ──────────────────────────────────────
# 注意：运行时创建 Agent 实例应使用本地 re-import（确保 reload 后拿到最新类）。
# 这里的顶层 import 仅用于类型标注。
from fp_core.core.agent import Agent
from fp_core.core.io import RestIO, WebSocketIO
from fp_core.core.lifecycle import HookContext, LifecycleHook
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin

# ════════════════════════════════════════════════════════════
# 1. EventBus — 异步发布/订阅
# ════════════════════════════════════════════════════════════


class EventBus:
    """
    异步事件总线，用于 Agent 生命周期事件 → WebSocket 的桥梁。

    支持多个订阅者（多个 WebSocket 连接），自动清理断开连接。

    背压保护：
      队列（maxsize=1024）满时不丢弃订阅者，而是丢弃最旧事件，
      保证订阅者始终能拿到最新事件，且不会失去连接。
    """

    def __init__(self):
        self._subscribers: dict[str, asyncio.Queue] = {}
        self._next_id = 0

    def subscribe(self) -> tuple[str, asyncio.Queue]:
        """订阅事件流，返回 (subscriber_id, queue)"""
        sub_id = f"sub_{self._next_id}"
        self._next_id += 1
        q: asyncio.Queue = asyncio.Queue(maxsize=1024)
        self._subscribers[sub_id] = q
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        """取消订阅"""
        self._subscribers.pop(sub_id, None)

    async def publish(self, event: dict):
        """向所有订阅者推送事件

        背压策略：队列满时丢弃最旧事件（get_nowait），而非丢弃订阅者。
        确保订阅者不会因消费慢而被静默移除。
        """
        dead_subs: list[str] = []
        for sub_id, q in self._subscribers.items():
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()  # 丢弃最旧事件
                    q.put_nowait(event)  # 重试放入最新事件
                    get_logger().warning(f"[EventBus] ⚠️ 订阅者 {sub_id} 队列满，已丢弃最旧事件")
                except (asyncio.QueueEmpty, asyncio.QueueFull):
                    dead_subs.append(sub_id)  # 保护性断开
        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)
            get_logger().warning(f"[EventBus] ⚠️ 订阅者 {sub_id} 因队列异常已被断开")

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def shutdown(self):
        """关闭所有订阅者"""
        dead_subs = list(self._subscribers.keys())
        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)


# 全局事件总线实例
event_bus = EventBus()


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
# 2. WebUIPlugin — 生命周期桥接
# ════════════════════════════════════════════════════════════


class WebUIPlugin(Plugin):
    """
    WebUI 桥接插件

    监听 Agent 的关键生命周期钩子，将中间状态（思考、工具调用、错误等）
    通过 EventBus 实时推送到前端。

    不修改 Agent 核心代码，以插件形式运行时自动激活。
    """

    name = "webui_bridge"
    version = "1.0.0"

    def on_register(self, lifecycle):
        """注册所有需要监听的生命周期钩子"""
        lifecycle.register(
            LifecycleHook.ON_BEFORE_LLM_CALL,
            self._on_before_llm,
            priority=5,
            name="webui_before_llm",
        )
        lifecycle.register(
            LifecycleHook.ON_AFTER_LLM_CALL,
            self._on_after_llm,
            priority=5,
            name="webui_after_llm",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_SELECT,
            self._on_tool_select,
            priority=5,
            name="webui_tool_select",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_CALL,
            self._on_tool_call,
            priority=5,
            name="webui_tool_call",
        )
        lifecycle.register(
            LifecycleHook.ON_TOOL_RESULT,
            self._on_tool_result,
            priority=5,
            name="webui_tool_result",
        )
        lifecycle.register(
            LifecycleHook.ON_ERROR,
            self._on_error,
            priority=5,
            name="webui_error",
        )
        # 注意：不监听 ON_BEFORE_RESPONSE — 前端统一从 done.final_content 获取最终回复
        # 避免与 WebSocketIO·say() / process_and_notify 重复推送
        lifecycle.register(
            LifecycleHook.ON_SHUTDOWN,
            self._on_shutdown,
            priority=5,
            name="webui_shutdown",
        )

    async def _emit(self, event_type: str, **data):
        """向 EventBus 发布事件"""
        await event_bus.publish({"type": event_type, "ts": time.time(), **data})

    async def _on_before_llm(self, ctx: HookContext, **kwargs):
        """LLM 调用开始 → 前端显示"思考中"状态"""
        await self._emit("llm_start")

    async def _on_after_llm(self, ctx: HookContext, **kwargs):
        """LLM 调用完成 → 通知前端 LLM 状态

        注意：LLM 可能同时返回文本内容和工具调用（如"我来查一下..." + tool_calls）。
        文本内容通过 llm_end.content 推送，前端在其已存在的分支中消费。
        最终的 done.final_content 只包含最后一条纯文本回复，不包含中间输出。
        """
        content = kwargs.get("content", "")
        await self._emit(
            "llm_end",
            content=content,
            has_tool_calls=kwargs.get("has_tool_calls", False),
            tool_names=kwargs.get("tool_names", []),
        )

    async def _on_tool_select(self, ctx: HookContext, **kwargs):
        """工具选择 → 前端显示即将调用的工具列表"""
        tools = kwargs.get("tools", [])
        await self._emit("tool_select", tools=tools)

    async def _on_tool_call(self, ctx: HookContext, **kwargs):
        """工具调用开始 → 前端显示工具名称和参数"""
        await self._emit(
            "tool_call",
            name=kwargs.get("tool_name", ""),
            args=kwargs.get("tool_args", ""),
            tool_call_id=kwargs.get("tool_call_id", ""),
        )

    async def _on_tool_result(self, ctx: HookContext, **kwargs):
        """工具调用完成 → 前端显示结果摘要"""
        result = kwargs.get("result", "")
        await self._emit(
            "tool_result",
            name=kwargs.get("tool_name", ""),
            result=(result[:200] + "...") if len(result) > 200 else result,
            tool_call_id=kwargs.get("tool_call_id", ""),
        )

    async def _on_error(self, ctx: HookContext, **kwargs):
        """错误发生 → 前端显示错误信息"""
        await self._emit("error", error=str(kwargs.get("error", "")))

    async def _on_shutdown(self, ctx: HookContext, **kwargs):
        """Agent 关闭 → 前端显示关闭通知"""
        await self._emit("shutdown")

    def on_unregister(self):
        """卸载插件时清理资源"""
        # WebUIPlugin 是桥接插件，随 Agent 生命周期自动管理，
        # EventBus 由 WebUI 服务器全局管理，此处无需额外清理
        pass


# ════════════════════════════════════════════════════════════
# 3. FastAPI 应用
# ════════════════════════════════════════════════════════════

# ── 全局 Agent 实例 ──────────────────────────────────────
_agent: Agent | None = None
_agent_lock = asyncio.Lock()


async def get_agent() -> Agent:
    """获取或创建全局 Agent 实例（延迟初始化）

    每次创建前需本地 re-import Agent 类，确保 reload 后用的是新版。
    """
    global _agent
    if _agent is None:
        async with _agent_lock:
            if _agent is None:
                # 本地 import：即使 reload 后模块缓存已更新，这里取到的总是最新类
                from fp_core.core.agent import Agent as _AgentClass

                _agent = _AgentClass(enable_log=False)
                # 注册 WebUI 桥接插件
                webui_plugin = WebUIPlugin()
                _agent.plugins.register(webui_plugin)
                await _agent.ensure_initialized()
                get_logger().info(f"[WebUI] Agent 已初始化 (model={_agent.model})")
    return _agent


# ── 生命周期管理 ─────────────────────────────────────────


@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化 Agent，关闭时清理"""
    get_logger().info("[WebUI] 🚀 Five Pebbles WebUI 启动中...")

    # Agent 延迟初始化，第一次请求时创建
    yield

    # 关闭
    get_logger().info("[WebUI] 🛑 正在关闭...")
    global _agent
    if _agent is not None:
        await _agent.shutdown()
    await event_bus.shutdown()
    get_logger().info("[WebUI] ✅ 已关闭")


# ── FastAPI 实例 ─────────────────────────────────────────

app = FastAPI(
    title="Five Pebbles WebUI",
    description="五块卵石 AI Agent 的 Web 界面",
    version="1.0.0",
    lifespan=lifespan,
)


# ── 认证中间件 ──────────────────────────────────────────

_AUTH_WHITELIST = {"/api/auth", "/api/health"}


@app.middleware("http")
async def auth_middleware(request: Request, call_next):
    """拦截 /api/* 请求，验证 Bearer Token（白名单除外）"""
    path = request.url.path
    if path.startswith("/api/") and path not in _AUTH_WHITELIST:
        auth = request.headers.get("authorization", "")
        expected = f"Bearer {_WEBUI_TOKEN}"
        if not auth or auth != expected:
            return JSONResponse(status_code=401, content={"detail": "未授权，请先登录"})
    return await call_next(request)


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
async def auth_login(request: Request, body: dict):
    """验证 Token 并登录（每 IP 限频）"""
    client_ip = request.client.host if request.client else request.headers.get("x-forwarded-for", "unknown")
    _check_auth_rate_limit(client_ip)
    token = body.get("token", "").strip()
    if secrets.compare_digest(token, _WEBUI_TOKEN):
        return {"status": "ok", "message": "验证通过"}
    raise HTTPException(status_code=401, detail="Token 无效")


@app.get("/api/health")
async def health_check():
    """健康检查端点"""
    agent = await get_agent()
    return {
        "status": "ok",
        "agent": agent.model,
        "session": agent.session.session_id,
        "subscribers": event_bus.subscriber_count,
    }


@app.post("/api/chat")
async def send_message(body: dict):
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

    agent = await get_agent()

    # 发送消息时启动一个独立的后台任务来处理
    # 前端通过 WebSocket 接收实时更新
    # 使用 RestIO 避免交互式命令（如 /back 无参数）阻塞
    response = await agent.process(message, io=RestIO())

    return {
        "response": response.content,
        "session_id": agent.session.session_id,
    }


@app.get("/api/sessions")
async def list_sessions():
    """列出所有历史会话"""
    agent = await get_agent()
    sessions = agent.session.list_sessions()

    result = []
    for sid, info in sorted(sessions.items(), key=lambda x: x[1].get("created", ""), reverse=True):
        result.append({
            "id": sid,
            "message_count": info.get("message_count", 0),
            "created": info.get("created", ""),
            "summary": info.get("summary", ""),
            "is_current": sid == agent.session.session_id,
        })

    return {"sessions": result}


@app.get("/api/commands")
async def list_commands():
    """返回快捷命令列表（从 fp-core 命令注册表动态获取，自动适配新增命令）"""
    from fp_core.commands import get_all_commands

    cmds = get_all_commands()
    return {"commands": [{"name": f"/{name}", "desc": desc} for name, desc in cmds.items()]}


# ════════════════════════════════════════════════════════════
# 4a. Agent 替换核心逻辑（新建 / 重载共享）
# ════════════════════════════════════════════════════════════


async def _replace_agent(
    *,
    reload_modules: bool = False,
    restore_session: bool = True,
) -> dict:
    """
    替换全局 Agent 实例的核心逻辑。

    Args:
        reload_modules: 是否先热重载所有核心模块（/reload 用）
        restore_session: 是否恢复旧会话（/reload 用；新建 Agent 用 False）

    安全保证：
      - 如果 Agent 正在处理请求，返回 409 拒绝
      - 重载期间 _agent 被设为 None
      - 模块重载失败时 _agent 保持 None，get_agent() 自动创建新实例
      - 活跃的 WebSocket 连接保有旧 agent 对象引用，仍可继续工作
    """
    global _agent

    if _agent is not None and _agent.is_processing:
        raise HTTPException(status_code=409, detail="Agent 正在处理请求，请稍后重试")

    async with _agent_lock:
        # ── 保存旧会话并 shutdown 旧 Agent ──
        old_sid: str | None = None
        if _agent is not None:
            _agent.state.session.save_context(_agent.state.conversation.to_serializable())
            old_sid = _agent.state.session.session_id
            with suppress(Exception):
                await _agent.shutdown()
            _agent = None

        # ── 通知前端准备重连 ──
        label = "重载" if reload_modules else "新建"
        await event_bus.publish({
            "type": "reload",
            "message": f"🔄 Agent 正在{label}，连接即将断开",
        })

        # ── 热重载所有模块（仅 /reload） ──
        if reload_modules:
            try:
                _reload_modules()
            except RuntimeError as e:
                get_logger().error(f"[WebUI] ❌ 模块重载失败: {e}")
                raise HTTPException(status_code=500, detail=f"模块重载失败: {e}") from e

        # ── 重新导入 Agent 类（确保获取最新代码） ──
        from fp_core.core.agent import Agent as NewAgent

        # ── 创建新 Agent ──
        try:
            _agent = NewAgent(enable_log=False)
            _agent.plugins.register(WebUIPlugin())
            await _agent.ensure_initialized()
        except Exception as e:
            get_logger().error(f"[WebUI] ❌ 新 Agent 创建失败: {e}")
            _agent = None
            raise HTTPException(status_code=500, detail=f"新 Agent 创建失败: {e}") from e

        # ── 会话管理：恢复旧会话 / 创建新会话 ──
        from fp_core.core.prompt_builder import PromptBuilder

        if restore_session and old_sid:
            try:
                _agent.state.session.switch_session(old_sid)
                prompt = PromptBuilder().build_system_prompt()
                _agent.state.conversation.reset(prompt)
                saved = _agent.state.session.load_context(prompt)
                if len(saved) > 1:
                    _agent.state.conversation.replace_all(saved)
                get_logger().info(f"[WebUI] 🔄 已恢复会话: {old_sid}")
            except Exception as e:
                get_logger().warning(f"[WebUI] ⚠️ 会话恢复失败: {e}")
        else:
            # 新 Agent 构造函数已创建会话，只需重建 context
            try:
                prompt = PromptBuilder().build_system_prompt()
                _agent.state.conversation.reset(prompt)
                saved = _agent.state.session.load_context(prompt)
                if len(saved) > 1:
                    _agent.state.conversation.replace_all(saved)
                new_sid = _agent.state.session.session_id
                get_logger().info(f"[WebUI] 🆕 已使用新会话: {new_sid}")
            except Exception as e:
                get_logger().error(f"[WebUI] ❌ 新会话初始化失败: {e}")
                raise HTTPException(status_code=500, detail=f"新会话初始化失败: {e}") from e

        get_logger().info(
            f"[WebUI] {'🔄' if reload_modules else '🆕'} Agent {'重载' if reload_modules else '新建'}完成 "
            f"(model={_agent.model}, session={_agent.session.session_id})"
        )

        # ── 稍等片刻，让前端收到 reload 事件后再推送 done ──
        await asyncio.sleep(0.3)
        await event_bus.publish({
            "type": "reload_done",
            "session_id": _agent.session.session_id,
            "model": _agent.model,
        })

    return {
        "status": "ok",
        "session_id": _agent.session.session_id,
        "model": _agent.model,
    }


@app.post("/api/agent/new")
async def new_agent():
    """
    创建全新 Agent 实例（不重载模块、不恢复旧会话）。

    相当于 Agent 刚启动时的状态，所有内存状态被清空。
    """
    return await _replace_agent(reload_modules=False, restore_session=False)


@app.post("/api/sessions")
async def create_new_session():
    """创建新会话并切换到它"""
    agent = await get_agent()

    # 记录旧会话，用于后台生成摘要
    old_sid = agent.session.session_id
    old_context = agent.state.conversation.messages  # 浅拷贝

    # 保存当前会话上下文
    agent.state.session.save_context(agent.state.conversation.to_serializable())

    # 创建新会话（自动切换到新会话）
    new_sid = agent.session.create_session()

    # 重建 agent 上下文（加载 system prompt 到新会话）
    from fp_core.core.prompt_builder import PromptBuilder

    prompt = PromptBuilder().build_system_prompt()
    agent.state.conversation.reset(prompt)
    saved = agent.state.session.load_context(prompt)
    if len(saved) > 1:
        agent.state.conversation.replace_all(saved)

    # 同步生成旧会话摘要（不传 tools，确保 LLM 返回纯文本标题）
    history_msgs = [m for m in old_context if m["role"] != "system"]
    if len(history_msgs) >= 2:
        try:
            summary_msgs = old_context + [
                {"role": "user", "content": "请总结一下，给这次对话起一个5到10个汉字的名字。不要添加任何多余的文字。"}
            ]
            response = await agent.client.chat.completions.create(
                model=agent.model,
                messages=summary_msgs,
                temperature=0.3,
                max_tokens=32,
                extra_body={"enable_thinking": False},
            )
            summary = response.choices[0].message.content or ""
            summary = summary.strip().strip('"').strip("'").strip("「」『』")
            if not summary or len(summary) > 50:
                # 回退：取首条用户消息
                for m in history_msgs:
                    if m["role"] == "user":
                        text = m.get("content", "").strip()
                        if text:
                            summary = text.split("\n")[0].strip()[:50]
                            break
            if not summary:
                summary = "empty_session"
            agent.session.update_meta(old_sid, summary=summary)
        except Exception:
            pass

    return {"session_id": new_sid, "status": "created"}


@app.delete("/api/sessions/{session_id}")
async def delete_session_endpoint(session_id: str):
    """删除指定会话（不能是当前会话）"""
    agent = await get_agent()

    # 检查是不是当前会话
    if session_id == agent.session.session_id:
        raise HTTPException(status_code=400, detail="不能删除当前正在使用的会话")

    if not agent.session.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或删除失败")

    return {"status": "deleted", "session_id": session_id}


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """
    获取指定会话的完整消息列表（直接读文件，不修改 Agent 状态）。

    ⚠️ index = 非 system 消息的 1-based 索引（与 /back 命令的索引体系一致）。
    跳过 role=system 的消息（如 compact 产生的摘要），因为 /back 命令
    使用的是 get_non_system_messages()，两类 system 消息不计入：
      1. 原始 system prompt
      2. compact 产生的摘要 system 消息
      3. repair_tool_ordering 转化的孤儿 tool 消息
    """
    from fp_core.core.session import _session_path

    path = _session_path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")

    messages = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if msg.get("__meta__"):
                continue
            messages.append(msg)

    # ── 只对非 system 消息编号（与 ConversationState.back() 的索引规则一致） ──
    # ConversationState.get_non_system_messages() 只返回 role != "system" 的消息，
    # 所以 compact 后产生的 system(摘要) 消息不计入索引。
    # 如果按文件全部消息编号，compact/resume 后前端 data-index 与后端索引会错位。
    result = []
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
async def search_session_messages(session_id: str, body: dict):
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
    from fp_core.core.session import _session_path

    path = _session_path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")

    query = body.get("query", "").strip()
    if not query:
        raise HTTPException(status_code=400, detail="查询内容不能为空")

    import re

    use_regex = body.get("regex", False)
    limit = min(body.get("limit", 20), 100)

    # ── 编译匹配模式 ──
    pattern: re.Pattern | None = None
    query_lower: str | None = None
    if use_regex:
        try:
            pattern = re.compile(query)
        except re.error as e:
            raise HTTPException(status_code=400, detail=f"正则表达式无效: {e}") from e
    else:
        query_lower = query.lower()

    # ── 逐行扫描文件 ──
    results = []
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
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if msg.get("__meta__"):
            continue

        role = msg.get("role", "")
        content = msg.get("content", "")

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
    agent = await get_agent()

    # 记录旧会话，用于后台生成摘要
    old_sid = agent.session.session_id
    old_context = agent.state.conversation.messages  # 浅拷贝

    # 保存当前会话
    agent.state.session.save_context(agent.state.conversation.to_serializable())

    if not agent.session.switch_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")

    # 加载目标会话的消息到内存上下文
    _prompt = agent.state.conversation.system_prompt
    _saved = agent.session.load_context(_prompt)
    if _saved:
        agent.state.conversation.set_messages(_prompt, _saved)

    # 同步生成旧会话摘要（不传 tools）
    history_msgs = [m for m in old_context if m["role"] != "system"]
    if len(history_msgs) >= 2:
        try:
            summary_msgs = old_context + [
                {"role": "user", "content": "请总结一下，给这次对话起一个5到10个汉字的名字。不要添加任何多余的文字。"}
            ]
            response = await agent.client.chat.completions.create(
                model=agent.model,
                messages=summary_msgs,
                temperature=0.3,
                max_tokens=32,
                extra_body={"enable_thinking": False},
            )
            summary = response.choices[0].message.content or ""
            summary = summary.strip().strip('"').strip("'").strip("「」『』")
            if not summary or len(summary) > 50:
                for m in history_msgs:
                    if m["role"] == "user":
                        text = m.get("content", "").strip()
                        if text:
                            summary = text.split("\n")[0].strip()[:50]
                            break
            if not summary:
                summary = "empty_session"
            agent.session.update_meta(old_sid, summary=summary)
        except Exception:
            pass

    return {
        "session_id": session_id,
        "status": "switched",
    }


@app.post("/api/sessions/clear")
async def clear_current_session():
    """清空当前会话"""
    agent = await get_agent()
    agent.session.clear_session_file()
    _prompt = agent.state.conversation.system_prompt
    agent.state.conversation.reset(_prompt)
    return {"status": "cleared"}


# ════════════════════════════════════════════════════════════
# 4b. 热重载 Agent
# ════════════════════════════════════════════════════════════

# ── 需要热重载的模块列表（按依赖顺序，子模块由父模块自动重新导入）─
_RELOAD_MODULES = [
    # 第 1 层：无项目内部依赖
    "fp_core.config",
    # 第 2 层：依赖 config
    "fp_core.core.io",
    "fp_core.core.lifecycle",
    "fp_core.core.session",
    "fp_core.core.llm_client",
    # 第 3 层：依赖 core.*
    "fp_core.plugins.base.plugin",
    "fp_core.prompts.agent",
    # 第 4 层：工具和命令（含全局注册表状态）
    "fp_core.commands",  # _discover_commands() 重新扫描
    "fp_core.tools",  # ToolRegistry 全局实例重建
    # 第 5 层：Agent 主干（依赖以上所有）
    "fp_core.core.agent",
]


def _reload_modules():
    """按顺序 reload 所有核心模块，返回是否成功。"""
    import importlib

    # ── 先 reload tools 和 commands 的子模块 ──
    # tools/commands 的父模块 reload 时会重新扫描子模块，
    # 但子模块本身的代码可能被用户修改，所以需要先 reload 子模块
    for prefix in ("fp_core.tools.", "fp_core.commands.", "fp_core.core."):
        for mod_name in list(sys.modules.keys()):
            if (
                mod_name.startswith(prefix) and mod_name in sys.modules and mod_name not in _RELOAD_MODULES
            ):  # 父模块由主列表处理
                importlib.reload(sys.modules[mod_name])

    # ── 按依赖顺序 reload 主模块 ──
    for mod_name in _RELOAD_MODULES:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception as e:
                raise RuntimeError(f"重载模块 {mod_name} 失败: {e}") from e

    importlib.invalidate_caches()


@app.post("/api/reload")
async def reload_agent():
    """
    热重载 Agent：刷新所有核心代码后创建新 Agent，保留会话上下文。

    与 `/api/agent/new` 的区别：
      - 本接口 importlib.reload 所有核心模块（命令/工具/LLM 客户端等）
      - 本接口会恢复旧会话
    """
    return await _replace_agent(reload_modules=True, restore_session=True)


# ════════════════════════════════════════════════════════════
# 5. WebSocket 端点 — 流式聊天
# ════════════════════════════════════════════════════════════


@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, token: str | None = Query(None)):
    """
    WebSocket 流式聊天

    连接后，前端可发送 JSON 消息：
      {"type": "message", "content": "你好"}

    服务器通过 WebSocket 推送实时事件：
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
    """
    await websocket.accept()

    # ── 验证 Token ──
    if not token or not secrets.compare_digest(token, _WEBUI_TOKEN):
        await websocket.send_json({"type": "error", "error": "未授权，请先登录"})
        await websocket.close(code=4001)
        return

    # 订阅事件总线
    sub_id, event_queue = event_bus.subscribe()

    # 当前连接的 IO 通道（用于交互式命令）
    current_io: WebSocketIO | None = None

    # 后台任务跟踪（初始化后供 finally 安全清理）
    push_task: asyncio.Task | None = None
    process_tasks: list[asyncio.Task] = []

    try:
        # 发送连接确认
        await websocket.send_json({"type": "connected", "sub_id": sub_id})

        # 后台任务：读取 EventBus 并推送至 WebSocket
        async def push_events():
            while True:
                try:
                    event = await asyncio.wait_for(event_queue.get(), timeout=30)
                    await websocket.send_json(event)
                except TimeoutError:
                    # 心跳保活
                    try:
                        await websocket.send_json({"type": "ping"})
                    except Exception:
                        break
                except Exception:
                    break

        push_task = asyncio.create_task(push_events())

        # 主循环：接收客户端消息
        # 首次获取 Agent 引用（后续每次消息前检查是否已被重载）
        agent = await get_agent()
        # process_tasks 已在函数顶部初始化

        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)

            # ── 检测 Agent 是否已被重载（热替换）──
            # 如果 get_agent() 返回了不同的对象，说明发生了 reload/new_agent
            # 旧 WS 连接透明切换到新 Agent 引用，避免断开重连导致消息丢失。
            # push_events 任务已通过 EventBus 收到 reload/reload_done 事件，
            # 前端此时已显示"已重载"状态，无需再发额外通知。
            current_agent = await get_agent()
            if current_agent is not agent:
                get_logger().info("[WebUI] ↻ 旧 WS 透明切换到新 Agent（reload 后无缝续传）")
                agent = current_agent

            if data.get("type") == "message":
                content = data.get("content", "").strip()
                if not content:
                    await websocket.send_json({"type": "error", "error": "消息不能为空"})
                    continue

                # ── 如果 IO 通道正在等待用户输入，直接注入回复 ──
                if current_io and current_io.feed_reply(content):
                    continue

                # ── 如果 IO 通道还在运行（非 ask 状态），拒绝 ──
                if current_io and current_io.is_running:
                    await websocket.send_json({
                        "type": "error",
                        "error": "正在处理中，请等待当前操作完成",
                    })
                    continue

                # ── 正常处理：创建新 IO 通道并启动处理任务 ──
                ws_io = WebSocketIO(event_bus)
                ws_io.is_running = True
                current_io = ws_io

                async def process_and_notify(msg: str, io: WebSocketIO, agent=agent):
                    """处理消息并通过 EventBus 推送结果"""
                    try:
                        response = await agent.process(msg, io=io)
                        # ── 热重载检测：/reload 命令已将新 Agent 存入 state._reload_result ──
                        # CLI (fp-terminal) 和 ACP 在 process() 返回后都有同样的检测逻辑。
                        # 此处必须同步检查并交换全局 _agent 引用，否则下次消息会发给已 shutdown
                        # 的旧 Agent，导致 I/O 静默失效。
                        reload_data = getattr(agent.state, "_reload_result", None)
                        if reload_data is not None:
                            new_agent, info = reload_data
                            agent.state._reload_result = None  # 防止重复消费
                            global _agent
                            _agent = new_agent
                            get_logger().info(
                                f"[WebUI] 🔄 WebSocket 通道已切换到新 Agent "
                                f"(model={info['model']}, session={info['session_id']})"
                            )
                            agent = new_agent  # 让后续代码使用新 Agent 的引用
                        # 检查是否被用户主动中断（工具执行中 task.cancel()）
                        # agent._cancelled_by_user 在 agent._process_inner 的
                        # except 块中被设为 True，process() 返回后检查此标记。
                        # 用这种方式而非重新抛出 CancelledError，是为了不破坏
                        # CLI 模式——CLI 的 except CancelledError: break 会退出程序。
                        if agent.cancelled_by_user:
                            agent.reset_cancelled()
                            await event_bus.publish({"type": "cancelled"})
                        else:
                            # 从后端获取权威的非 system 消息计数，传递给前端
                            # 前端据此校准 liveMsgIndex，消除前端自增计数器漂移
                            all_msgs = agent.state.conversation.messages
                            non_sys_count = sum(1 for m in all_msgs if m.get("role") != "system")
                            await event_bus.publish({
                                "type": "done",
                                "session_id": agent.session.session_id,
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

                task = asyncio.create_task(process_and_notify(content, ws_io))
                process_tasks.append(task)

            elif data.get("type") == "cancel":
                # 用户请求中断 → 取消最近的处理任务
                # task.cancel() 注入 CancelledError → agent._process_inner 的
                # tool 执行 except 块捕获 → 标记 _cancelled_by_user = True
                # → process() 正常返回 → process_and_notify 检查标记 → 发布 cancelled。
                # 不重新抛出异常，避免 CLI 的 except CancelledError: break 误退出。
                while process_tasks:
                    task = process_tasks.pop()
                    if not task.done():
                        task.cancel()
                        break

            elif data.get("type") == "ping":
                await websocket.send_json({"type": "pong"})

    except WebSocketDisconnect:
        pass
    except Exception as e:
        with suppress(Exception):
            await websocket.send_json({"type": "error", "error": str(e)})
    finally:
        # ── 清理后台任务：取消事件推送和处理任务 ──
        # 防止在 Agent 重载后孤立任务继续使用已关闭的客户端
        if push_task is not None:
            push_task.cancel()
        for t in process_tasks:
            t.cancel()
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
    <head><meta charset="utf-8"><title>Five Pebbles WebUI</title></head>
    <body style="background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;
          display:flex;align-items:center;justify-content:center;height:100vh;">
      <div style="text-align:center">
        <h1> Five Pebbles WebUI</h1>
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


def main():
    """启动 WebUI 服务器"""
    parser = argparse.ArgumentParser(description="Five Pebbles WebUI")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址（默认 127.0.0.1）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发用）")
    parser.add_argument("--expose", action="store_true", help="监听 0.0.0.0，允许局域网设备访问")
    args = parser.parse_args()

    if args.expose:
        args.host = "0.0.0.0"

    print()
    print("🤖 Five Pebbles WebUI")
    print()
    if args.host == "0.0.0.0":
        get_logger().warning("  ⚠️  已监听 0.0.0.0，局域网设备可访问此服务")
        get_logger().warning("  ⚠️  请妥善保管 Token，建议使用 HTTPS 反向代理")
        print()
    get_logger().info(f"  🌐  WebUI: http://{args.host}:{args.port}")
    get_logger().info(f"  🔌  WS:    ws://{args.host}:{args.port}/ws/chat")
    get_logger().info(f"  📡  API:   http://{args.host}:{args.port}/api/health")
    print()
    # 显示 Token（从文件读，确保与文件一致）
    display_token = _load_or_create_token()
    get_logger().info(f"  🔑  启动 Token: ...{display_token[-4:]}")
    get_logger().info(f"  📄  Token 文件: {_TOKEN_FILE}  （cat 查看完整 Token）")
    print()

    uvicorn.run(
        "fp_webui.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
