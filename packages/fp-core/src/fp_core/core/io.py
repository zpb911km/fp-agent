"""
IO 通道抽象 — 解耦 CLI / WebUI 的输入输出

架构：
  ┌──────────────┐    命令层使用      ┌──────────────┐
  │  命令/Agent  │ ──────────────→   │  IOChannel   │
  │  (业务逻辑)   │ ←──────────────  │  (抽象协议)   │
  └──────────────┘                  └──────┬───────┘
                                           │
                              ┌────────────┼────────────┐
                              ▼            ▼            ▼
                         ┌────────┐  ┌──────────┐  ┌────────────┐
                         │ CLIIO │  │WSIO      │  │ 测试 Mock  │
                         │(term) │  │EventBus  │  │  (注入用)  │
                         │       │  │+ Queue   │  │            │
                         └────────┘  └──────────┘  └────────────┘

职责：
  - info/warning/error: 输出各类级别文本消息
  - thinking_start/stop: LLM 思考中的动画/状态指示
  - stream_think: 流式输出 LLM 思考过程 token（灰色）
  - stream_write/end: 流式输出 LLM 回复内容
  - tool_call/tool_result: 显示工具调用和结果

CLIIO 实现已在 fp-terminal/packages/fp_cli/cli_io.py 中。
"""

import asyncio
import uuid
from typing import Any


class IOChannel:
    """
    IO 通道抽象基类。

    命令和 Agent 内部方法通过此接口与用户交互，
    不直接依赖 input() 或 display 模块。
    """

    # 前端身份标识。子类覆盖：
    #   CLIIO → "terminal" / WebSocketIO → "webui" / ACPIO → "acp"
    # 供扩展（命令/插件/工具）判断运行环境，避免各自 hack 检测。
    frontend: str = "unknown"

    # ── 文本输出 ─────────────────────────────────────

    def info(self, text: str):
        """输出信息"""

    def warning(self, text: str):
        """输出警告（黄色）"""

    def error(self, text: str):
        """输出错误（红色）"""

    # ── 思考动画（Spinner） ──────────────────────────

    async def thinking_start(self, streaming: bool = False):
        """开始思考动画

        Args:
            streaming: True 表示后续会通过 stream_write/think 流式输出 token，
                       此时不应显示 spinner（token 本身就是进度指示）；
                       False 表示非流式（如 backward compat），应显示 spinner。
        """

    async def thinking_stop(self):
        """停止思考动画"""

    # ── 流式输出（LLMStreamer） ──────────────────────

    def stream_think(self, token: str):
        """写入思考过程 token（灰色/着色）"""

    def stream_write(self, content: str):
        """写入流式内容片段"""

    def stream_end(self):
        """结束流式输出"""

    def stream_reset(self):
        """异常恢复：强制重置流式输出状态（默认无操作）"""

    # ── 工具调用展示 ─────────────────────────────────

    def tool_call(self, name: str, args: dict[str, Any]):
        """显示工具调用"""

    def tool_result(self, result: str):
        """显示工具执行结果"""

    # ── 交互式输入 ───────────────────────────────────

    # ask 语义标记（子类覆盖）：
    #   False = 同步阻塞等待回答（CLI / WebUI）
    #   True  = 无带内回复通道：ask() 把问题**展示**给用户后立即返回 ""，
    #            回答将作为用户的下一条消息在新轮次到达（ACP）。
    # _ask_user 据此区分"用户未回答(空)"与"deferred 已展示待下轮"。
    ask_deferred: bool = False

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        """向用户提问，获取文本回复（结构化契约 v2）

        Args:
            prompt: 问题文本（清晰、自包含）
            options: 可选项列表 — 前端渲染为选择按钮/编号列表；
                     用户输入编号时由**展示层**解析为选项文本
            suggest: 推荐默认值 — 前端展示为推荐徽章，空回车采纳；
                     展示层在空输入时直接返回该值
            ask_id: 关联 id（收据/注入对账用）；缺省由实现生成

        返回用户原文（已是最终文本，调用方不再做编号/默认值解析）。
        空串 = 用户未回答 / 无交互通道（语义由调用方结合 ask_deferred 判定）。
        """
        raise NotImplementedError


class WebSocketIO(IOChannel):
    """
    WebSocket 通道 — 通过 EventBus 推送输出，等待用户回复。

    与 WebSocket 处理器配合使用：
      - WebSocketIO.thinking_start/stop 推送 thinking 事件
      - WebSocketIO.stream_write/end 推送 content 块事件
      - WebSocketIO.ask() 发布 "ask" 事件，阻塞等待 feed_reply()
      - WebSocket 处理器收到用户消息后调用 feed_reply()
    """

    frontend = "webui"

    def __init__(self, event_bus: Any):
        # event_bus: EventBus 类型定义在 fp-webui 包（fp_webui/main.py），
        # fp-core 不应依赖 fp-webui（分层方向错误），故用 Any 兜底。
        self._event_bus: Any = event_bus
        # ask_id → Future（v2：多 ask 对账 + 重连快照；旧单 future 已废）
        self._pending_replies: dict[str, asyncio.Future[str]] = {}
        # 当前等待中的 ask 元数据（供 snapshot 跨连接恢复）
        self._pending_ask_meta: dict[str, Any] | None = None
        self.is_running = False  # WebSocket 处理器用来判断当前是否有任务在处理

    # ── 供 WebSocket 处理器调用 ─────────────────────────

    def feed_reply(self, text: str, ask_id: str | None = None) -> bool:
        """
        注入用户回复。
        ask_id 指定 → 精确唤醒对应 ask（幂等：已答复/不存在 → False）；
        ask_id 缺省 → 唤醒最新的未答复 ask。
        若当前无等待者 → False（调用方按普通消息处理）。
        """
        if ask_id is not None:
            fut = self._pending_replies.get(ask_id)
            if fut is None or fut.done():
                return False
            fut.set_result(text)
            return True
        # 无 ask_id → 最新未答复者
        for fut in reversed(list(self._pending_replies.values())):
            if not fut.done():
                fut.set_result(text)
                return True
        return False

    def pending_ask_snapshot(self) -> dict[str, Any] | None:
        """供 session_runtime.snapshot：等待中的 ask 元数据（跨连接恢复组件）"""
        if self._pending_ask_meta is None:
            return None
        live = [f for f in self._pending_replies.values() if not f.done()]
        if not live:
            return None
        return dict(self._pending_ask_meta)

    # ── IO 接口 ─────────────────────────────────────────

    def _pub(self, type_: str, **data: Any) -> None:
        """向 EventBus 发布事件（fire-and-forget）"""
        asyncio.ensure_future(self._event_bus.publish({"type": type_, **data}))

    def info(self, text: str):
        self._pub("info", content=text)

    def warning(self, text: str):
        self._pub("warning", content=text)

    def error(self, text: str):
        self._pub("error", error=text)

    async def thinking_start(self, streaming: bool = False):
        self._pub("thinking", status="start")

    async def thinking_stop(self):
        self._pub("thinking", status="stop")

    def stream_write(self, content: str):
        self._pub("chunk", content=content)

    def stream_think(self, token: str):
        self._pub("thinking_content", token=token)

    def stream_end(self):
        self._pub("stream_end")

    def stream_reset(self):
        """WebSocket 无状态管理，无需操作"""

    def tool_call(self, name: str, args: dict[str, Any]):
        self._pub("tool_call", name=name, args=args)

    def tool_result(self, result: str):
        self._pub("tool_result", result=result)

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        ask_id = ask_id or uuid.uuid4().hex[:8]
        fut: asyncio.Future[str] = asyncio.get_running_loop().create_future()
        self._pending_replies[ask_id] = fut
        self._pending_ask_meta = {
            "ask_id": ask_id,
            "prompt": prompt,
            "options": list(options or []),
            "suggest": suggest,
            "version": 2,
        }
        # 事件 schema v2：结构化 ask（version 字段供旧前端兼容分支）
        await self._event_bus.publish({"type": "ask", **self._pending_ask_meta})
        try:
            return await fut
        finally:
            self._pending_replies.pop(ask_id, None)
            if self._pending_ask_meta and self._pending_ask_meta.get("ask_id") == ask_id:
                self._pending_ask_meta = None


class RestIO(IOChannel):
    """
    REST 通道 — 无交互能力的静默通道。

    REST 请求是单次请求-响应模式，无法做多轮交互。
    若命令触发了 ask()（如 /back 无参数交互模式），
    直接返回空字符串触发"已取消"分支，不会阻塞。
    """

    frontend = "rest"

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        return ""

    # 所有显示方法都是 no-op
    def info(self, text: str):
        pass

    def warning(self, text: str):
        pass

    def error(self, text: str):
        pass

    async def thinking_start(self, streaming: bool = False):
        pass

    async def thinking_stop(self):
        pass

    def stream_write(self, content: str):
        pass

    def stream_think(self, token: str):
        pass

    def stream_end(self):
        pass

    def stream_reset(self):
        """静默通道，无需操作"""

    def tool_call(self, name: str, args: dict[str, Any]):
        pass

    def tool_result(self, result: str):
        pass
