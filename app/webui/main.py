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
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Optional

# ── 路径修复：确保能从项目根目录正确导入 ───────────────
_PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _PROJECT_ROOT not in sys.path:
    sys.path.insert(0, _PROJECT_ROOT)

# ── FastAPI / WebSocket ─────────────────────────────────
try:
    from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException, Query, Request
    from fastapi.responses import HTMLResponse, JSONResponse
    from fastapi.staticfiles import StaticFiles
    import uvicorn
except ImportError as e:
    print(f"[WebUI] 缺少依赖: {e}")
    print("  → 请安装: pip install 'fastapi[standard]' uvicorn")
    sys.exit(1)

# ── Agent 核心导入 ──────────────────────────────────────
from core.agent import Agent
from core.lifecycle import LifecycleHook, HookContext
from core.io import WebSocketIO
from plugins.base.plugin import Plugin, PluginConfig, PluginRegistry

import config as project_config
import display


# ════════════════════════════════════════════════════════════
# 1. EventBus — 异步发布/订阅
# ════════════════════════════════════════════════════════════

class EventBus:
    """
    异步事件总线，用于 Agent 生命周期事件 → WebSocket 的桥梁。
    
    支持多个订阅者（多个 WebSocket 连接），自动清理断开连接。
    """

    def __init__(self):
        self._subscribers: Dict[str, asyncio.Queue] = {}
        self._next_id = 0

    def subscribe(self) -> tuple[str, asyncio.Queue]:
        """订阅事件流，返回 (subscriber_id, queue)"""
        sub_id = f"sub_{self._next_id}"
        self._next_id += 1
        q: asyncio.Queue = asyncio.Queue(maxsize=256)
        self._subscribers[sub_id] = q
        return sub_id, q

    def unsubscribe(self, sub_id: str):
        """取消订阅"""
        self._subscribers.pop(sub_id, None)

    async def publish(self, event: dict):
        """向所有订阅者推送事件"""
        dead_subs: list[str] = []
        for sub_id, q in self._subscribers.items():
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                dead_subs.append(sub_id)  # 消费太慢，断开
        for sub_id in dead_subs:
            self._subscribers.pop(sub_id, None)

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

_TOKEN_FILE: str = os.path.join(_PROJECT_ROOT, ".webui_token")

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
        lifecycle.register(
            LifecycleHook.ON_BEFORE_RESPONSE,
            self._on_response,
            priority=5,
            name="webui_response",
        )
        lifecycle.register(
            LifecycleHook.ON_SHUTDOWN,
            self._on_shutdown,
            priority=5,
            name="webui_shutdown",
        )

    async def _emit(self, type: str, **data):
        """向 EventBus 发布事件"""
        await event_bus.publish({"type": type, "ts": time.time(), **data})

    async def _on_before_llm(self, ctx: HookContext, **kwargs):
        """LLM 调用开始 → 前端显示"思考中"状态"""
        await self._emit("llm_start")

    async def _on_after_llm(self, ctx: HookContext, **kwargs):
        """LLM 调用完成 → 前端显示回复内容"""
        await self._emit(
            "llm_end",
            content=kwargs.get("content", ""),
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
        )

    async def _on_tool_result(self, ctx: HookContext, **kwargs):
        """工具调用完成 → 前端显示结果摘要"""
        result = kwargs.get("result", "")
        await self._emit(
            "tool_result",
            name=kwargs.get("tool_name", ""),
            result=(result[:200] + "...") if len(result) > 200 else result,
        )

    async def _on_error(self, ctx: HookContext, **kwargs):
        """错误发生 → 前端显示错误信息"""
        await self._emit("error", error=str(kwargs.get("error", "")))

    async def _on_response(self, ctx: HookContext, **kwargs):
        """最终回复生成 → 前端显示完整回复"""
        await self._emit(
            "response",
            content=kwargs.get("content", ""),
        )

    async def _on_shutdown(self, ctx: HookContext, **kwargs):
        """Agent 关闭 → 前端显示关闭通知"""
        await self._emit("shutdown")


# ════════════════════════════════════════════════════════════
# 3. FastAPI 应用
# ════════════════════════════════════════════════════════════

# ── 全局 Agent 实例 ──────────────────────────────────────
_agent: Optional[Agent] = None
_agent_lock = asyncio.Lock()

async def get_agent() -> Agent:
    """获取或创建全局 Agent 实例（延迟初始化）"""
    global _agent
    if _agent is None:
        async with _agent_lock:
            if _agent is None:
                _agent = Agent(enable_log=False)
                # 注册 WebUI 桥接插件
                webui_plugin = WebUIPlugin()
                _agent.plugins.register(webui_plugin)
                await _agent.ensure_initialized()
                display.info(f"[WebUI] Agent 已初始化 (model={_agent.model})")
    return _agent


# ── 生命周期管理 ─────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    """FastAPI 生命周期：启动时初始化 Agent，关闭时清理"""
    display.info("[WebUI] 🚀 Five Pebbles WebUI 启动中...")
    display.info(f"[WebUI] 项目根目录: {_PROJECT_ROOT}")
    
    # Agent 延迟初始化，第一次请求时创建
    yield
    
    # 关闭
    display.info("[WebUI] 🛑 正在关闭...")
    global _agent
    if _agent is not None:
        await _agent.shutdown()
    await event_bus.shutdown()
    display.info("[WebUI] ✅ 已关闭")


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

@app.post("/api/auth")
async def auth_login(body: dict):
    """验证 Token 并登录"""
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
    response = await agent.process(message)
    
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


# ════════════════════════════════════════════════════════════
# 4a. 新建 Agent（shutdown 旧实例，创建全新实例）
# ════════════════════════════════════════════════════════════

@app.post("/api/agent/new")
async def new_agent():
    """
    创建全新 Agent 实例。
    
    流程：
      1. shutdown 旧 Agent（保存当前会话后优雅退出）
      2. 创建新 Agent 实例
      3. 注册 WebUIPlugin 桥接插件
      4. 创建全新的会话（清空上下文）
      5. 通过 EventBus 通知前端刷新
    
    这是真正的"重置"——所有内存状态被清空，所有模块被重新加载，
    相当于 Agent 刚启动时的状态。
    """
    global _agent

    # ── 检查是否正在处理 ──
    if _agent is not None and _agent.is_processing:
        raise HTTPException(status_code=409, detail="Agent 正在处理请求，请稍后重试")

    async with _agent_lock:
        # ── 保存旧会话并 shutdown 旧 Agent ──
        if _agent is not None:
            try:
                _agent.save_context()
                await _agent.shutdown()
            except Exception as e:
                display.warning(f"[WebUI] ⚠️ 旧 Agent shutdown 时发生异常: {e}")
            _agent = None

        # ── 通知前端准备重连 ──
        await event_bus.publish({
            "type": "reload",
            "message": "🔄 新建 Agent 中，连接即将断开",
        })

        # ── 重新导入 Agent 类（确保获取最新代码） ──
        from core.agent import Agent as NewAgent

        # ── 创建新 Agent ──
        try:
            _agent = NewAgent(enable_log=False)
            webui_plugin = WebUIPlugin()
            _agent.plugins.register(webui_plugin)
            await _agent.ensure_initialized()
        except Exception as e:
            display.error(f"[WebUI] ❌ 新 Agent 创建失败: {e}")
            _agent = None
            raise HTTPException(status_code=500, detail=f"新 Agent 创建失败: {e}")

        # ── 使用 Agent 构造函数已创建的新会话 ──
        # NewAgent() 的 SessionManager(resume=None) 中已调用 _init_session()
        # 生成了全新会话，此处只需重建 context 即可
        try:
            _agent.rebuild_context()
            new_sid = _agent.session.session_id
            display.info(f"[WebUI] 🆕 已使用新会话: {new_sid}")
        except Exception as e:
            display.error(f"[WebUI] ❌ 新会话初始化失败: {e}")
            raise HTTPException(status_code=500, detail=f"新会话初始化失败: {e}")

        display.info(
            f"[WebUI] 🆕 Agent 新建完成 "
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


@app.post("/api/sessions")
async def create_new_session():
    """创建新会话并切换到它"""
    agent = await get_agent()
    
    # 记录旧会话，用于后台生成摘要
    old_sid = agent.session.session_id
    old_context = agent.get_messages()  # 浅拷贝
    
    # 保存当前会话上下文
    agent.save_context()
    
    # 创建新会话（自动切换到新会话）
    new_sid = agent.session.create_session()
    
    # 重建 agent 上下文（加载 system prompt 到新会话）
    agent.rebuild_context()
    
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
                stream=False,
                temperature=0.3,
                max_tokens=32,
                extra_body={"enable_thinking": False},
            )
            summary = response.choices[0].message.content or ""
            summary = summary.strip().strip('"').strip("'").strip('「」『』')
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
    
    if not agent.delete_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在或删除失败")
    
    return {"status": "deleted", "session_id": session_id}


@app.get("/api/sessions/{session_id}")
async def get_session(session_id: str):
    """获取指定会话的完整历史"""
    agent = await get_agent()
    
    # 保存当前会话
    agent.save_context()
    
    # 加载目标会话
    if not agent.switch_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    
    history = []
    for msg in agent.get_messages()[1:]:  # 跳过 system
        entry = {
            "role": msg["role"],
            "content": msg.get("content", ""),
        }
        if msg.get("tool_calls"):
            entry["tool_calls"] = [
                {"name": tc["function"]["name"], "args": tc["function"]["arguments"]}
                for tc in msg["tool_calls"]
            ]
        history.append(entry)
    
    # 切回原会话
    agent.switch_session(agent.session.session_id)
    
    return {
        "session_id": session_id,
        "history": history,
    }


@app.get("/api/sessions/{session_id}/messages")
async def get_session_messages(session_id: str):
    """
    获取指定会话的完整消息列表（直接读文件，不修改 Agent 状态）。
    
    返回消息按 1-based 索引排列，与 /back 命令的编号一致。
    """
    from core.session import _session_path
    
    path = _session_path(session_id)
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    
    messages = []
    with open(path, "r", encoding="utf-8") as f:
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
    
    # 在每条消息中注入 1-based 索引（用于 /back 命令）
    result = []
    for i, msg in enumerate(messages):
        entry = {
            "index": i + 1,  # 1-based，与 /back 显示一致
            "role": msg.get("role", ""),
            "content": msg.get("content", ""),
            "tool_calls": msg.get("tool_calls"),
        }
        result.append(entry)
    
    return {
        "session_id": session_id,
        "total": len(result),
        "messages": result,
    }


@app.post("/api/sessions/{session_id}/switch")
async def switch_session_endpoint(session_id: str):
    """切换到指定会话"""
    agent = await get_agent()
    
    # 记录旧会话，用于后台生成摘要
    old_sid = agent.session.session_id
    old_context = agent.get_messages()  # 浅拷贝
    
    # 保存当前会话
    agent.save_context()
    
    if not agent.switch_session(session_id):
        raise HTTPException(status_code=404, detail=f"会话 {session_id} 不存在")
    
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
                stream=False,
                temperature=0.3,
                max_tokens=32,
                extra_body={"enable_thinking": False},
            )
            summary = response.choices[0].message.content or ""
            summary = summary.strip().strip('"').strip("'").strip('「」『』')
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
    agent.clear_session()
    return {"status": "cleared"}


# ════════════════════════════════════════════════════════════
# 4b. 热重载 Agent
# ════════════════════════════════════════════════════════════

# ── 需要热重载的模块列表（按依赖顺序，子模块由父模块自动重新导入）─
_RELOAD_MODULES = [
    # 第 1 层：无项目内部依赖
    'config',
    'display',
    # 第 2 层：依赖 config
    'core.io',
    'core.lifecycle',
    'core.session',
    'core.llm_client',
    # 第 3 层：依赖 core.*
    'plugins.base.plugin',
    'prompts.agent',
    'skills.loader',
    # 第 4 层：工具和命令（含全局注册表状态）
    'commands',        # _discover_commands() 重新扫描
    'tools',           # ToolRegistry 全局实例重建
    # 第 5 层：Agent 主干（依赖以上所有）
    'core.agent',
]


def _reload_modules():
    """按顺序 reload 所有核心模块，返回是否成功。"""
    import importlib

    # ── 先 reload tools 和 commands 的子模块 ──
    # tools/commands 的父模块 reload 时会重新扫描子模块，
    # 但子模块本身的代码可能被用户修改，所以需要先 reload 子模块
    for prefix in ('tools.', 'commands.', 'core.'):
        for mod_name in list(sys.modules.keys()):
            if mod_name.startswith(prefix) and mod_name in sys.modules:
                if mod_name not in _RELOAD_MODULES:  # 父模块由主列表处理
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
    热重载 Agent：在不重启服务器的前提下，刷新所有核心代码并创建新 Agent。

    流程：
      1. 保存当前会话上下文到文件
      2. 关闭旧 Agent（释放 LLM 客户端连接）
      3. importlib.reload 所有关键模块（按依赖顺序）
      4. 创建新 Agent 实例
      5. 注册 WebUIPlugin 桥接插件
      6. 恢复原会话上下文
      7. 替换全局 _agent 引用
      8. 通过 EventBus 通知各 WebSocket 客户端重连
    
    安全保证：
      - 如果 Agent 正在处理请求，返回 409 拒绝重载
      - 重载期间 _agent 被设为 None，get_agent() 会等待锁
      - 如果重载过程中任何模块 reload 失败，_agent 保持为 None，get_agent() 自动创建新实例
      - 活跃的 WebSocket 连接保有旧的 agent 对象引用，仍可继续工作
    """
    global _agent

    # ── 检查是否正在处理 ──
    if _agent is not None and _agent.is_processing:
        raise HTTPException(status_code=409, detail="Agent 正在处理请求，请稍后重试")

    async with _agent_lock:
        # ── 保存旧会话并关闭旧 Agent ──
        old_sid: Optional[str] = None
        if _agent is not None:
            _agent.save_context()
            old_sid = _agent.session.session_id
            try:
                await _agent.shutdown()
            except Exception:
                pass  # 静默容错
            _agent = None

        # ── 通知前端准备重连 ──
        await event_bus.publish({
            "type": "reload",
            "message": "🔄 Agent 正在重载，连接即将断开",
        })

        # ── 热重载所有模块 ──
        try:
            _reload_modules()
        except RuntimeError as e:
            display.error(f"[WebUI] ❌ 模块重载失败: {e}")
            # _agent 保持 None，后续请求会通过 get_agent() 自动创建
            raise HTTPException(status_code=500, detail=f"模块重载失败: {e}")

        # ── 重新导入 Agent 类 ──
        # 注意：main.py 顶部 from core.agent import Agent 是旧引用，
        # 必须重新 import 才能获得重载后的类
        from core.agent import Agent as NewAgent

        # ── 创建新 Agent ──
        try:
            _agent = NewAgent(enable_log=False)
            webui_plugin = WebUIPlugin()
            _agent.plugins.register(webui_plugin)
            await _agent.ensure_initialized()
        except Exception as e:
            display.error(f"[WebUI] ❌ 新 Agent 创建失败: {e}")
            _agent = None
            raise HTTPException(status_code=500, detail=f"新 Agent 创建失败: {e}")

        # ── 恢复旧会话 ──
        if old_sid:
            try:
                _agent.session.switch_session(old_sid)
                _agent.rebuild_context()
                display.info(f"[WebUI] 🔄 已恢复会话: {old_sid}")
            except Exception as e:
                display.warning(f"[WebUI] ⚠️ 会话恢复失败: {e}")

        display.info(
            f"[WebUI] 🔄 Agent 重载完成 "
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


# ════════════════════════════════════════════════════════════
# 5. WebSocket 端点 — 流式聊天
# ════════════════════════════════════════════════════════════

@app.websocket("/ws/chat")
async def websocket_chat(websocket: WebSocket, token: Optional[str] = Query(None)):
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
    current_io: Optional[WebSocketIO] = None
    
    # 后台任务跟踪（初始化后供 finally 安全清理）
    push_task: Optional[asyncio.Task] = None
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
                except asyncio.TimeoutError:
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
                display.info("[WebUI] ↻ 旧 WS 透明切换到新 Agent（reload 后无缝续传）")
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
                
                async def process_and_notify(msg: str, io: WebSocketIO):
                    """处理消息并通过 EventBus 推送结果"""
                    try:
                        response = await agent.process(msg, io=io)
                        # 检查是否被用户主动中断（工具执行中 task.cancel()）
                        # agent._cancelled_by_user 在 agent._process_inner 的
                        # except 块中被设为 True，process() 返回后检查此标记。
                        # 用这种方式而非重新抛出 CancelledError，是为了不破坏
                        # CLI 模式——CLI 的 except CancelledError: break 会退出程序。
                        if agent.cancelled_by_user:
                            agent.reset_cancelled()
                            await event_bus.publish({"type": "cancelled"})
                        else:
                            await event_bus.publish({
                                "type": "done",
                                "session_id": agent.session.session_id,
                                "final_content": response.content,
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
        try:
            await websocket.send_json({"type": "error", "error": str(e)})
        except Exception:
            pass
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
        with open(index_path, "r", encoding="utf-8") as f:
            return HTMLResponse(f.read())
    # 如果前端文件不存在，返回说明页面
    return HTMLResponse("""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"><title>Five Pebbles WebUI</title></head>
    <body style="background:#1a1a2e;color:#e0e0e0;font-family:sans-serif;display:flex;align-items:center;justify-content:center;height:100vh;">
      <div style="text-align:center">
        <h1>🪨 Five Pebbles WebUI</h1>
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
    parser.add_argument("--host", default="0.0.0.0", help="监听地址（默认 0.0.0.0）")
    parser.add_argument("--port", type=int, default=8765, help="监听端口（默认 8765）")
    parser.add_argument("--reload", action="store_true", help="启用热重载（开发用）")
    args = parser.parse_args()

    print()
    display.print_logo()
    print()
    display.info(f"  🌐  WebUI: http://{args.host}:{args.port}")
    display.info(f"  🔌  WS:    ws://{args.host}:{args.port}/ws/chat")
    display.info(f"  📡  API:   http://{args.host}:{args.port}/api/health")
    print()
    # 显示 Token（从文件读，确保与文件一致）
    display_token = _load_or_create_token()
    display.info(f"  🔑  启动 Token: {display_token}")
    display.info(f"  📄  已写入: {_TOKEN_FILE}")
    print()

    uvicorn.run(
        "app.webui.main:app",
        host=args.host,
        port=args.port,
        reload=args.reload,
        log_level="info",
    )


if __name__ == "__main__":
    main()
