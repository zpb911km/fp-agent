"""TUI 行为测试 — n6 流式渲染 / n7 提交分流与斜杠禁用 / n8 AskModal

全部用 textual run_test pilot（headless），portal 以桩替身注入，
不连 LLM。重点锁定任务 #156 的交互契约：
- 忙碌时斜杠被拒（n3 决策的前端面）
- 忙碌时普通输入走 submit 队列且视觉回显 ⏳
- ask 选项卡返回选项原文 / 编号 / 推荐值 / Esc 空串
"""

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_cli.tui import TUIIO, AskModal, FPApp


class _FakeStatus:
    model = "fake-model"
    session_id = "sess-0123456789abcdef"
    is_processing = False
    is_run_active = False
    cancelled = False


class _FakePortal:
    """portal 桩：记录 send/submit 调用，可切换忙碌态。"""

    def __init__(self, busy: bool = False) -> None:
        self.busy = busy
        self.sent: list[str] = []
        self.submitted: list[str] = []
        self._status = _FakeStatus()

    @property
    def is_open(self) -> bool:
        return True

    @property
    def run(self):  # noqa: ANN201 — 桩对象
        return self

    @property
    def status(self):  # noqa: ANN201
        st = self._status
        st.is_run_active = self.busy
        return st

    async def send(self, text: str, io: object = None):  # noqa: ANN001
        del io
        self.sent.append(text)

        class _Resp:
            content = ""

        return _Resp()

    async def submit(self, text: str, io: object = None) -> str:  # noqa: ANN001
        del io
        self.submitted.append(text)
        return "queued"

    def cancel(self) -> None:
        pass

    @property
    def transcript(self) -> list[dict[str, str]]:
        return []


async def _submit_text(app: FPApp, text: str) -> None:
    """直接驱动提交 handler（绕过键盘注入的时序不确定性）。"""
    from textual.widgets import Input

    prompt = app.query_one("#prompt", Input)
    prompt.value = text
    # 构造 Submitted 事件走同一 handler
    await app._on_submit(Input.Submitted(prompt, text))  # noqa: SLF001


class TestSubmitRouting(unittest.TestCase):
    """n7 — 空闲 send / 忙碌 queue / 忙碌拒斜杠"""

    def _run(self, busy: bool, text: str) -> _FakePortal:
        from fp_core.api import portal as real_portal

        fake = _FakePortal(busy=busy)

        async def _scenario() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            # is_open 是类 property；run 是实例属性——分别打桩
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    await _submit_text(app, text)
                    await pilot.pause(0.05)

        asyncio.run(_scenario())
        return fake

    def test_idle_routes_to_send(self) -> None:
        fake = self._run(busy=False, text="hello")
        self.assertEqual(fake.sent, ["hello"])
        self.assertEqual(fake.submitted, [])

    def test_busy_routes_to_submit(self) -> None:
        fake = self._run(busy=True, text="interjection")
        self.assertEqual(fake.submitted, ["interjection"])
        self.assertEqual(fake.sent, [])

    def test_busy_rejects_slash(self) -> None:
        fake = self._run(busy=True, text="/help")
        self.assertEqual(fake.sent, [])
        self.assertEqual(fake.submitted, [])


class TestAskModal(unittest.TestCase):
    """n8 — 选项卡/编号/推荐值/Esc 的解析语义（与 CLI 一致）"""

    def _resolve(self, modal: AskModal, *, choose: str | None = None, raw: str | None = None) -> str:
        result: list[str] = []

        def _capture(value: str) -> None:
            result.append(value)

        modal._resolve_for_test = _capture  # type: ignore[attr-defined]  # 测试旁路
        if choose is not None:
            # 直接走 parse（选项语义）：模拟 OptionSelected 携选项原文
            modal.dismiss = choose  # type: ignore[method-assign]
            return choose
        return modal._parse(raw or "")  # noqa: SLF001

    def test_empty_returns_suggest(self) -> None:
        m = AskModal("pick", ["a", "b"], suggest="b")
        self.assertEqual(m._parse(""), "b")  # noqa: SLF001

    def test_empty_no_suggest_returns_empty(self) -> None:
        m = AskModal("pick", ["a"], suggest="")
        self.assertEqual(m._parse(""), "")  # noqa: SLF001

    def test_digit_maps_to_option(self) -> None:
        m = AskModal("pick", ["alpha", "beta"], suggest="")
        self.assertEqual(m._parse("2"), "beta")  # noqa: SLF001
        self.assertEqual(m._parse("1"), "alpha")  # noqa: SLF001

    def test_out_of_range_digit_is_free_text(self) -> None:
        m = AskModal("pick", ["alpha"], suggest="")
        self.assertEqual(m._parse("9"), "9")  # noqa: SLF001

    def test_free_text_passthrough(self) -> None:
        m = AskModal("pick", None, suggest="")
        self.assertEqual(m._parse("custom answer"), "custom answer")  # noqa: SLF001

    def test_no_options_ignores_digits(self) -> None:
        m = AskModal("q", None, suggest="x")
        self.assertEqual(m._parse("1"), "1")  # 无选项时编号不是索引


class TestAskModalPilot(unittest.TestCase):
    """pilot 全链路：AskRequest → modal → dismiss → future resolve"""

    def test_ask_roundtrip_via_option(self) -> None:

        async def _scenario() -> str:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[str] = loop.create_future()
                from fp_cli.tui import AskRequest

                app.post_message(AskRequest("选一个", ["甲", "乙"], "", fut))
                await pilot.pause(0.05)
                # modal 已推入：通过 Input 提交编号
                screen = app.screen
                self.assertIsInstance(screen, AskModal)
                screen._on_input(type("E", (), {"value": "2"})())  # noqa: SLF001  # 同步 handler
                return await asyncio.wait_for(fut, timeout=2)

        result = asyncio.run(_scenario())
        self.assertEqual(result, "乙")


class TestStreamingRender(unittest.TestCase):
    """n6 — 流式 token 合批进 live Markdown，finalize 后固化"""

    def test_stream_blocks_render(self) -> None:
        async def _scenario() -> tuple[int, bool]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                baseline = len(app.query_one("#chat").children)
                io.stream_think("让我想想")
                io.stream_write("# 标题\n内容")
                io.stream_end()
                await pilot.pause(0.05)
                after = len(app.query_one("#chat").children)
                return baseline, after

        baseline, after = asyncio.run(_scenario())
        # think 段 + content 段各固化为一个子件（finalize 不新增）
        self.assertGreaterEqual(after - baseline, 2)


if __name__ == "__main__":
    unittest.main()
