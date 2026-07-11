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
                         │input()│  │EventBus  │  │  (注入用)  │
                         │display│  │+ Queue   │  │            │
                         └────────┘  └──────────┘  └────────────┘

职责：
  - info/hint/warning/error/item: 输出各类级别的文本消息
  - thinking_start/stop: LLM 思考中的动画/状态指示
  - stream_write/end: 流式输出 LLM 回复内容
  - tool_call/tool_result: 显示工具调用和结果
"""

import asyncio


class IOChannel:
    """
    IO 通道抽象基类。

    命令和 Agent 内部方法通过此接口与用户交互，
    不直接依赖 input() 或 display 模块。
    """

    # ── 文本输出 ─────────────────────────────────────

    def info(self, text: str):
        """输出信息（绿色高亮）"""

    def hint(self, text: str):
        """输出提示（灰色）"""

    def warning(self, text: str):
        """输出警告（黄色）"""

    def error(self, text: str):
        """输出错误（红色）"""

    def item(self, text: str):
        """输出列表项（灰色缩进）"""

    # ── 思考动画（Spinner） ──────────────────────────

    async def thinking_start(self):
        """开始思考动画"""

    async def thinking_stop(self):
        """停止思考动画"""

    # ── 流式输出（LLMStreamer） ──────────────────────

    def stream_write(self, content: str):
        """写入流式内容片段"""

    def stream_end(self):
        """结束流式输出"""

    def stream_reset(self):
        """异常恢复：强制重置流式输出状态（默认无操作）"""

    # ── 工具调用展示 ─────────────────────────────────

    def tool_call(self, name: str, args: dict):
        """显示工具调用"""

    def tool_result(self, result: str):
        """显示工具执行结果"""

    # ── 交互式输入 ───────────────────────────────────

    async def ask(self, prompt: str) -> str:
        """向用户提问，获取文本回复"""
        raise NotImplementedError


class CLIIO(IOChannel):
    """
    CLI 通道 — 直接使用 input() 和 display 模块。

    保持现有的终端交互体验（着色、缩进等）。
    """

    # ── 文本输出 ─────────────────────────────────────

    def info(self, text: str):
        from fp_core import display as d

        d.info(text)

    def hint(self, text: str):
        from fp_core import display as d

        d.hint(text)

    def warning(self, text: str):
        from fp_core import display as d

        d.warning(text)

    def error(self, text: str):
        from fp_core import display as d

        d.error(text)

    def item(self, text: str):
        from fp_core import display as d

        d.item(text)

    # ── 思考动画 ─────────────────────────────────────

    async def thinking_start(self):
        from fp_core import display as d

        self._spinner = d.Spinner("思考中")
        await self._spinner.start()

    async def thinking_stop(self):
        if hasattr(self, "_spinner") and self._spinner:
            await self._spinner.stop()
            self._spinner = None

    # ── 流式输出 ─────────────────────────────────────

    def stream_write(self, content: str):
        from fp_core import display as d

        if not hasattr(self, "_streamer") or self._streamer is None:
            self._streamer = d.LLMStreamer(silent=False)
        self._streamer.write(content)

    def stream_reset(self):
        """异常恢复：强制重置流式输出状态，下次 stream_write 会重建"""
        self._streamer = None

    def stream_end(self):
        if hasattr(self, "_streamer") and self._streamer:
            self._streamer.end()
            self._streamer = None

    # ── 工具调用展示 ─────────────────────────────────

    def tool_call(self, name: str, args: dict):
        import json

        from fp_core import display as d

        safe_args = {k: str(v) for k, v in args.items()}
        d.llm_tool(f"  🛠️  {name}({json.dumps(safe_args, ensure_ascii=False)})")

    def tool_result(self, result: str):
        from fp_core import display as d

        d.llm_tool(f"  📋  {result.strip()}")

    # ── 交互式输入 ───────────────────────────────────

    async def ask(self, prompt: str) -> str:
        from fp_core import display as d

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: input(prompt).strip())
            return result
        except (EOFError, KeyboardInterrupt):
            d.info("")
            return ""


class WebSocketIO(IOChannel):
    """
    WebSocket 通道 — 通过 EventBus 推送输出，等待用户回复。

    与 WebSocket 处理器配合使用：
      - WebSocketIO.thinking_start/stop 推送 thinking 事件
      - WebSocketIO.stream_write/end 推送 content 块事件
      - WebSocketIO.ask() 发布 "ask" 事件，阻塞等待 feed_reply()
      - WebSocket 处理器收到用户消息后调用 feed_reply()
    """

    def __init__(self, event_bus):
        self._event_bus = event_bus
        self._pending_reply: asyncio.Future | None = None
        self.is_running = False  # WebSocket 处理器用来判断当前是否有任务在处理

    # ── 供 WebSocket 处理器调用 ─────────────────────────

    def feed_reply(self, text: str) -> bool:
        """
        注入用户回复。
        若当前有 ask() 在等待，则唤醒它并返回 True；
        否则返回 False（无等待者）。
        """
        if self._pending_reply is not None and not self._pending_reply.done():
            self._pending_reply.set_result(text)
            return True
        return False

    # ── IO 接口 ─────────────────────────────────────────

    def _pub(self, type_: str, **data):
        """向 EventBus 发布事件（fire-and-forget）"""
        asyncio.ensure_future(self._event_bus.publish({"type": type_, **data}))

    def info(self, text: str):
        self._pub("info", content=text)

    def hint(self, text: str):
        self._pub("hint", content=text)

    def warning(self, text: str):
        self._pub("warning", content=text)

    def error(self, text: str):
        self._pub("error", error=text)

    def item(self, text: str):
        self._pub("item", content=text)

    async def thinking_start(self):
        self._pub("thinking", status="start")

    async def thinking_stop(self):
        self._pub("thinking", status="stop")

    def stream_write(self, content: str):
        self._pub("chunk", content=content)

    def stream_end(self):
        self._pub("stream_end")

    def stream_reset(self):
        """WebSocket 无状态管理，无需操作"""

    def tool_call(self, name: str, args: dict):
        self._pub("tool_call", name=name, args=args)

    def tool_result(self, result: str):
        self._pub("tool_result", result=result)

    async def ask(self, prompt: str) -> str:
        self._pending_reply = asyncio.get_running_loop().create_future()
        await self._event_bus.publish({"type": "ask", "prompt": prompt})
        try:
            result = await self._pending_reply
            return result
        finally:
            self._pending_reply = None


class RestIO(IOChannel):
    """
    REST 通道 — 无交互能力的静默通道。

    REST 请求是单次请求-响应模式，无法做多轮交互。
    若命令触发了 ask()（如 /back 无参数交互模式），
    直接返回空字符串触发"已取消"分支，不会阻塞。
    """

    async def ask(self, prompt: str) -> str:
        return ""

    # 所有显示方法都是 no-op
    def info(self, text: str):
        pass

    def hint(self, text: str):
        pass

    def warning(self, text: str):
        pass

    def error(self, text: str):
        pass

    def item(self, text: str):
        pass

    async def thinking_start(self):
        pass

    async def thinking_stop(self):
        pass

    def stream_write(self, content: str):
        pass

    def stream_end(self):
        pass

    def stream_reset(self):
        """静默通道，无需操作"""

    def tool_call(self, name: str, args: dict):
        pass

    def tool_result(self, result: str):
        pass
