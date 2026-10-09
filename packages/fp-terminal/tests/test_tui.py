"""TUI 测试 — 输入路由 / 降级链 / 图表原语 / 历史读写 / pilot 冒烟

对应任务 #156 n5：L0 判定与 L0 内部核心不变量。
pilot 冒烟用 textual 的 run_test（headless，无需真 TTY）。
"""

from __future__ import annotations

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_cli.charts import bar_rows, spark_segments
from fp_cli.history import append_history, load_history
from fp_cli.tui import TUIIO, route_input, tui_available


class TestRouteInput(unittest.TestCase):
    """路由真值表 — 锁定 n3 决策：忙碌禁斜杠、忙碌走队列"""

    def test_truth_table(self) -> None:
        self.assertEqual(route_input("", False), "ignore")
        self.assertEqual(route_input("   ", True), "ignore")
        self.assertEqual(route_input("/help", False), "command")
        self.assertEqual(route_input("/help", True), "reject_slash")
        self.assertEqual(route_input("/x", True), "reject_slash")
        self.assertEqual(route_input("hello", False), "send")
        self.assertEqual(route_input("hello", True), "queue")
        # 判定前 strip：前导空白斜杠同斜杠
        self.assertEqual(route_input(" /help", False), "command")
        self.assertEqual(route_input(" /help", True), "reject_slash")


class TestTuiAvailable(unittest.TestCase):
    """L0 判定：FP_NO_TUI / TERM=dumb 必须拒绝并给出可读原因"""

    def test_fp_no_tui_env(self) -> None:
        old = os.environ.get("FP_NO_TUI")
        os.environ["FP_NO_TUI"] = "1"
        try:
            ok, reason = tui_available()
            self.assertFalse(ok)
            self.assertIn("FP_NO_TUI", reason)
        finally:
            if old is None:
                del os.environ["FP_NO_TUI"]
            else:
                os.environ["FP_NO_TUI"] = old

    def test_term_dumb(self) -> None:
        # pytest 自身非 TTY，TTY 检查会先命中——给 stdin/stdout 打桩隔离 TERM 分支
        from unittest.mock import patch

        old = os.environ.get("TERM")
        os.environ["TERM"] = "dumb"

        class _TTY:
            @staticmethod
            def isatty() -> bool:
                return True

        try:
            with patch.object(sys, "stdin", _TTY()), patch.object(sys, "stdout", _TTY()):
                ok, reason = tui_available()
                self.assertFalse(ok)
                self.assertIn("dumb", reason)
        finally:
            if old is None:
                del os.environ["TERM"]
            else:
                os.environ["TERM"] = old

    def test_textual_importable(self) -> None:
        # textual 是 fp-terminal 运行时依赖：必须可导入（L0 前提之一）
        import textual

        self.assertTrue(textual.__version__)


class TestCharts(unittest.TestCase):
    """图表原语 — 零依赖渲染确定性"""

    def test_bar_rows_alignment_and_zero(self) -> None:
        rows = bar_rows({"a": 1, "b": 4})
        self.assertEqual(len(rows), 2)
        # 峰值行满条
        self.assertIn("█" * 24, rows[1])
        # 全零不崩、不出 ZeroDivision
        rows0 = bar_rows({"a": 0, "b": 0})
        self.assertEqual(len(rows0), 2)
        self.assertEqual(bar_rows({}), ["（无数据）"])

    def test_spark_segments(self) -> None:
        self.assertEqual(spark_segments([]), "")
        self.assertEqual(spark_segments([5]), "█")  # 峰值即最大段
        out = spark_segments([1, 2, 3])
        self.assertEqual(len(out), 3)
        self.assertEqual(spark_segments([0, 0]), "▁▁")


class TestHistory(unittest.TestCase):
    """历史读写 — FileHistory 格式兼容（+timestamp 行分隔）"""

    def test_roundtrip(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "sub", "_input_history")
            append_history(path, "first")
            append_history(path, "second line")
            append_history(path, "   ")  # 空白不落盘
            got = load_history(path)
            self.assertEqual(got, ["first", "second line"])

    def test_load_missing_file(self) -> None:
        self.assertEqual(load_history("/nonexistent/nowhere/_h"), [])


class TestTUIIOBuffering(unittest.TestCase):
    """挂载前缓冲 — app 未就绪时输出进 pending，FIFO 冲刷"""

    def test_pending_fifo_before_attach(self) -> None:
        io = TUIIO()
        io.info("a")
        io.warning("b")
        io.error("c")
        pending = io.take_pending()
        self.assertEqual([m.kind for m in pending], ["info", "warning", "error"])
        self.assertEqual([m.payload for m in pending], ["a", "b", "c"])
        # 冲刷后为空（幂等）
        self.assertEqual(io.take_pending(), [])

    def test_stream_buffers_flush_as_blocks(self) -> None:
        io = TUIIO()
        io.stream_think("thinking…")
        io.stream_write("hello ")
        io.stream_write("world")
        pending = io.take_pending()
        kinds = [m.kind for m in pending]
        self.assertEqual(kinds, ["think", "content", "finalize"])
        self.assertEqual(pending[1].payload, "hello world")

    def test_tool_call_flushes_stream_first(self) -> None:
        io = TUIIO()
        io.stream_write("before tool")
        io.tool_call("bash", {"command": "ls"})
        pending = io.take_pending()
        self.assertEqual([m.kind for m in pending], ["content", "tool"])


class TestPilotSmoke(unittest.TestCase):
    """pilot 冒烟 — headless 启动 TUI，验证挂载与欢迎渲染。

    不连真 LLM：portal 未 open，状态条/提交路径需优雅降级（is_open=False 分支）。
    """

    def test_mount_and_route(self) -> None:
        import asyncio

        from fp_cli.tui import FPApp

        async def _run() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                # 欢迎屏已渲染
                self.assertTrue(app.mounted)
                chat = app.query_one("#chat")
                self.assertGreater(len(chat.children), 1)
                # 空输入不产生动作
                self.assertEqual(route_input("", app._is_busy()), "ignore")  # noqa: SLF001

        asyncio.run(_run())


class TestDetachedHitRace(unittest.TestCase):
    """上游 #6643 竞态回归 — Markdown.update 摘块窗口内 MouseDown 不得崩溃。

    机制（官方复现 #6643）：update 不 await → 旧块已 detach 但 compositor
    命中图仍指向它 → _forward_event 取 parent=None → 'region' AttributeError。
    修复：FPDefaultScreen.get_widget_and_offset_at 将 detached 命中降为 miss
    （等价上游 PR #6645，上游合并发布后本测试应继续通过、修复可退场）。
    """

    def test_race_window_treated_as_miss(self) -> None:
        import asyncio

        from textual.app import App, ComposeResult
        from textual.containers import VerticalScroll
        from textual.events import MouseDown, MouseUp
        from textual.widgets import Markdown

        from fp_cli.tui import FPDefaultScreen

        class _RaceApp(App[None]):
            """最小载体：默认屏 = FPDefaultScreen + 单个 Markdown。"""

            def get_default_screen(self):  # noqa: ANN201 — 对齐 textual 签名
                return FPDefaultScreen(id="_default")

            def compose(self) -> ComposeResult:
                yield VerticalScroll(Markdown("# Before\n\nClick target paragraph.", id="document"))

        async def _run() -> None:
            app = _RaceApp()
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                doc = app.query_one("#document", Markdown)
                old = doc.query("MarkdownParagraph").first()
                x, y = old.region.x + 1, old.region.y
                # 更新不 await：制造摘块换块窗口
                doc.update("\n\n".join(f"replacement {i}" for i in range(200)))
                # 竞态窗口内命中必须是 miss（不得返回 detached 块）
                for step in range(100_000):
                    await asyncio.sleep(0)
                    if old.parent is None:
                        hit, _ = app.screen.get_widget_and_offset_at(x, y)
                        self.assertIsNone(
                            hit,
                            f"detached 块仍被命中 (step={step})：修复未生效",
                        )
                        break
                else:
                    self.skipTest("竞态窗口未捕获（事件循环调度差异）")
                # 窗口内投递点击：修复前此处崩溃
                app.screen.post_message(
                    MouseDown(
                        None,
                        x=x,
                        y=y,
                        button=1,
                        delta_x=0,
                        delta_y=0,
                        shift=False,
                        meta=False,
                        ctrl=False,
                    )
                )
                app.screen.post_message(
                    MouseUp(
                        None,
                        x=x,
                        y=y,
                        button=1,
                        delta_x=0,
                        delta_y=0,
                        shift=False,
                        meta=False,
                        ctrl=False,
                    )
                )
                await pilot.pause()

        asyncio.run(_run())


if __name__ == "__main__":
    unittest.main()
