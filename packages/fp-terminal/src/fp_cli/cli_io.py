"""
CLIIO — CLI 通道实现（fp-terminal 版）

通过 display 模块和 input() 实现终端交互。
"""

import asyncio
from typing import Any, Protocol, cast

from fp_cli import display as _display_mod
from fp_cli.display import LLMStreamer
from fp_core.api import IOChannel


class _StreamerToolProto(Protocol):
    """流式输出器 tool 接口。

    仅用于补全 display.LLMStreamer.tool 的签名（display.py 中 args 未注解），
    避免 reportUnknownMemberType 级联。
    """

    async def tool(self, name: str, args: dict[str, Any]) -> None: ...


class CLIIO(IOChannel):
    """CLI 通道 — 直接使用 input() 和 display 模块。"""

    frontend = "terminal"

    def __init__(self):
        self._streamer = None
        self._spinner = None
        # 未落盘的展示任务（tool_call/tool_result 的 create_task 句柄）。
        # ask() 渲染问题块前先等待这些任务完成 — 否则工具行的异步
        # 流式输出（逐段 0.02s 延迟）会插进问题块与输入光标之间。
        self._pending_disp: set[asyncio.Task[Any]] = set()

    def _spawn_disp(self, coro: Any) -> None:
        task = asyncio.create_task(coro)
        self._pending_disp.add(task)
        task.add_done_callback(self._pending_disp.discard)

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
        if self._streamer is None:
            self._streamer = LLMStreamer(silent=_display_mod._FP_SILENT)  # type: ignore[reportPrivateUsage]
        self._streamer.think(token)

    def stream_write(self, content: str):
        if self._streamer is None:
            self._streamer = LLMStreamer(silent=_display_mod._FP_SILENT)  # type: ignore[reportPrivateUsage]
        self._streamer.write(content)

    def stream_reset(self):
        self._streamer = None

    def stream_end(self):
        if self._streamer:
            self._streamer.end()
            self._streamer = None

    # ── 工具调用展示 ─────────────────────────────────

    def tool_call(self, name: str, args: dict[str, Any]):
        if self._streamer is None:
            self._streamer = LLMStreamer(silent=_display_mod._FP_SILENT)  # type: ignore[reportPrivateUsage]
        self._spawn_disp(cast(_StreamerToolProto, self._streamer).tool(name, args))

    def tool_result(self, result: str):
        if self._streamer is None:
            self._streamer = LLMStreamer(silent=_display_mod._FP_SILENT)  # type: ignore[reportPrivateUsage]
        self._spawn_disp(self._streamer.tool_result_line(result))

    # ── 交互式输入 ───────────────────────────────────

    async def ask(
        self,
        prompt: str,
        *,
        options: list[str] | None = None,
        suggest: str = "",
        ask_id: str | None = None,
    ) -> str:
        """结构化问答（契约 v2）：问题块渲染 + 编号/默认值解析。

        - options → 编号列表；用户可输入编号或自由文本
        - suggest → 推荐徽章；空回车采纳
        - 解析在展示层完成，返回的已是最终文本
        """

        def _read() -> str:
            lines = ["", f"❓ {prompt}"]
            for i, opt in enumerate(options or [], 1):
                marker = " （推荐）" if suggest and opt == suggest else ""
                lines.append(f"   {i}. {opt}{marker}")
            if suggest:
                lines.append(f"   💡 推荐默认值：{suggest}（直接回车采纳）")
            print("\n".join(lines))
            return input("❯ ").strip()

        # ── 显示时序（返工 n9）：防止工具行挤占回答区 ──
        # ① 先等已入队的展示任务落盘 — tool_call 是 create_task 异步调度
        #    （含逐段 0.02s 流式延迟），不等它会插进问题块与输入光标之间；
        # ② 再持工具行锁渲染问题块并读输入 — 问答挂起期间新到的
        #    tool_call/tool_result 在锁外排队，答完按序打印，回答区不被插队。
        if self._pending_disp:
            await asyncio.wait(list(self._pending_disp), timeout=2.0)

        lock = LLMStreamer._get_tool_lock()  # type: ignore[reportPrivateUsage]
        await lock.acquire()
        try:
            loop = asyncio.get_running_loop()
            try:
                raw = await loop.run_in_executor(None, _read)
            except (EOFError, KeyboardInterrupt):
                return ""
        finally:
            lock.release()

        if not raw:
            return suggest  # 空回车 = 采纳推荐值（无推荐值时返回空 = 未回答）
        if options and raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(options):
                return options[idx - 1]  # 编号 → 选项原文
        return raw
