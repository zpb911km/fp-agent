"""
CLIIO — CLI 通道实现（fp-terminal 版）

通过 display 模块和 input() 实现终端交互。
"""

import asyncio

from fp_core.core.io import IOChannel


class CLIIO(IOChannel):
    """CLI 通道 — 直接使用 input() 和 display 模块。"""

    def __init__(self):
        self._streamer = None
        self._spinner = None

    # ── 文本输出 ─────────────────────────────────────

    def info(self, text: str):
        from fp_cli import display as d

        d.info(text)

    def warning(self, text: str):
        from fp_cli import display as d

        d.warning(text)

    def error(self, text: str):
        from fp_cli import display as d

        d.error(text)

    # ── 思考动画 ─────────────────────────────────────

    async def thinking_start(self, streaming: bool = False):
        if streaming:
            # 流式模式下不显示 spinner，token 本身即是进度指示
            self._spinner = None
            return
        from fp_cli import display as d

        self._spinner = d.Spinner("思考中")
        await self._spinner.start()

    async def thinking_stop(self):
        if self._spinner:
            await self._spinner.stop()
            self._spinner = None

    # ── 流式输出 ─────────────────────────────────────

    def stream_think(self, token: str):
        from fp_cli import _FP_SILENT
        from fp_cli import display as d

        if self._streamer is None:
            self._streamer = d.LLMStreamer(silent=_FP_SILENT)
        self._streamer.think(token)

    def stream_write(self, content: str):
        from fp_cli import _FP_SILENT
        from fp_cli import display as d

        if self._streamer is None:
            self._streamer = d.LLMStreamer(silent=_FP_SILENT)
        self._streamer.write(content)

    def stream_reset(self):
        self._streamer = None

    def stream_end(self):
        if self._streamer:
            self._streamer.end()
            self._streamer = None

    # ── 工具调用展示 ─────────────────────────────────

    def tool_call(self, name: str, args: dict):
        from fp_cli import _FP_SILENT
        from fp_cli import display as d

        if self._streamer is None:
            self._streamer = d.LLMStreamer(silent=_FP_SILENT)
        asyncio.create_task(self._streamer.tool(name, args))

    def tool_result(self, result: str):
        from fp_cli import display as d

        d.llm_tool(f"  📋  {result.strip()}")

    # ── 交互式输入 ───────────────────────────────────

    async def ask(self, prompt: str) -> str:
        loop = asyncio.get_running_loop()
        try:
            result = await loop.run_in_executor(None, lambda: input(prompt).strip())
            return result
        except (EOFError, KeyboardInterrupt):
            return ""
