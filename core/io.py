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
"""

import asyncio


class IOChannel:
    """
    IO 通道抽象基类。

    命令和 Agent 内部方法通过此接口与用户交互，
    不直接依赖 input() 或 display 模块。
    """

    async def ask(self, prompt: str) -> str:
        """向用户提问，获取文本回复"""
        raise NotImplementedError

    def say(self, text: str):
        """输出普通信息"""

    def info(self, text: str):
        """输出信息（绿色高亮）"""

    def hint(self, text: str):
        """输出提示（灰色）"""

    def error(self, text: str):
        """输出错误（红色）"""

    def item(self, text: str):
        """输出列表项（灰色缩进）"""


class CLIIO(IOChannel):
    """
    CLI 通道 — 直接使用 input() 和 display 模块。

    保持现有的终端交互体验（着色、缩进等）。
    """

    async def ask(self, prompt: str) -> str:
        """阻塞等待用户终端输入"""
        import display as d

        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: input(prompt).strip())
            return result
        except (EOFError, KeyboardInterrupt):
            d.info("")
            return ""

    def say(self, text: str):
        import display as d

        d.info(text)

    def info(self, text: str):
        import display as d

        d.info(text)

    def hint(self, text: str):
        import display as d

        d.hint(text)

    def error(self, text: str):
        import display as d

        d.error(text)

    def item(self, text: str):
        import display as d

        d.item(text)


class WebSocketIO(IOChannel):
    """
    WebSocket 通道 — 通过 EventBus 推送输出，等待用户回复。

    与 WebSocket 处理器配合使用：
      - WebSocketIO.push_events() 将输出推送到前端
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

    async def ask(self, prompt: str) -> str:
        """发布 ask 事件，等待用户通过 WebSocket 回复"""
        self._pending_reply = asyncio.get_running_loop().create_future()
        await self._event_bus.publish({"type": "ask", "prompt": prompt})
        try:
            result = await self._pending_reply
            return result
        finally:
            self._pending_reply = None

    def _pub(self, type_: str, **data):
        """向 EventBus 发布事件（fire-and-forget）"""
        asyncio.ensure_future(self._event_bus.publish({"type": type_, **data}))

    def say(self, text: str):
        self._pub("say", content=text)

    def info(self, text: str):
        self._pub("info", content=text)

    def hint(self, text: str):
        self._pub("hint", content=text)

    def error(self, text: str):
        self._pub("error", error=text)

    def item(self, text: str):
        self._pub("item", content=text)


class RestIO(IOChannel):
    """
    REST 通道 — 无交互能力的静默通道。

    REST 请求是单次请求-响应模式，无法做多轮交互。
    若命令触发了 ask()（如 /back 无参数交互模式），
    直接返回空字符串触发"已取消"分支，不会阻塞。
    """

    async def ask(self, prompt: str) -> str:
        return ""

    def say(self, text: str):
        pass

    def info(self, text: str):
        pass

    def hint(self, text: str):
        pass

    def error(self, text: str):
        pass

    def item(self, text: str):
        pass
