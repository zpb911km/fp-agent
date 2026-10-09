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

from fp_cli.tui import TUIIO, AskModal, FPApp, PromptInput


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
    prompt = app.query_one("#prompt", PromptInput)
    prompt.text = text
    await app._handle_input(text)  # noqa: SLF001 — 与 Shift+Enter/发送钮同一入口


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


class TestMultilinePrompt(unittest.TestCase):
    """输入区升级契约：Enter 换行 / Shift+Enter 提交 / 发送钮提交 / 空文忽略"""

    def _scenario(self, *, press: str | None = None, click: bool = False, initial: str = "") -> list[str]:
        from fp_core.api import portal as real_portal

        fake = _FakePortal(busy=False)

        async def _run() -> list[str]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    prompt = app.query_one("#prompt", PromptInput)
                    prompt.focus()
                    if initial:
                        prompt.text = initial
                    if press is not None:
                        await pilot.press(*press.split())
                    if click:
                        await pilot.click("#send_btn")
                    await pilot.pause(0.1)
                    # 返回残留文本（提交会清空）+ 焦点归属
                    focused = app.focused.id if app.focused is not None else None
                    return fake.sent + fake.submitted + ["__text__:" + prompt.text, f"__focus__:{focused}"]

        return asyncio.run(_run())  # type: ignore[no-any-return]

    def test_enter_inserts_newline(self) -> None:
        out = self._scenario(press="enter", initial="第一行")
        self.assertIn("__text__:\n第一行", out)  # 换行而非提交
        self.assertNotIn("第一行", [x for x in out if not x.startswith("__text__")])

    def test_shift_enter_sends(self) -> None:
        out = self._scenario(press="shift+enter", initial="你好")
        self.assertIn("你好", out)
        self.assertIn("__text__:", out)  # 提交后清空

    def test_send_button_sends(self) -> None:
        out = self._scenario(click=True, initial="点我发送")
        self.assertIn("点我发送", out)
        self.assertIn("__focus__:prompt", out)  # 点击后焦点夺回输入框

    def test_empty_shift_enter_ignored(self) -> None:
        out = self._scenario(press="shift+enter")
        real = [x for x in out if not x.startswith("__")]
        self.assertEqual(real, [])
        self.assertIn("__text__:", out)


class TestInputLayout(unittest.TestCase):
    """#157 视觉契约（设计：glm-5.3）：
    - 输入框高度随内容自动增长，max 6 行封顶
    - 发送钮无文字（↑）、宽 3、高度跟随输入框（100% 对齐）
    - 输入行只保留上下边框、无背景
    """

    def _scenario(self, n_enters: int) -> dict[str, object]:
        from fp_core.api import portal as real_portal

        fake = _FakePortal(busy=False)

        async def _run() -> dict[str, object]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    prompt = app.query_one("#prompt", PromptInput)
                    prompt.focus()
                    for _ in range(n_enters):
                        await pilot.press("enter")
                    await pilot.pause(0.1)
                    btn = app.query_one("#send_btn")
                    row = app.query_one("#prompt_row")
                    # Textual 8.x：size=内容区，region 含边框（真实渲染高度看 region）
                    return {
                        "prompt_h": prompt.region.height,
                        "btn_h": btn.region.height,
                        "btn_w": btn.region.width,
                        "btn_label": btn.label,
                        "row_h": row.region.height,
                        "row_border_top": str(row.styles.border_top),
                        "row_border_left": str(row.styles.border_left),
                        "row_bg": str(row.styles.background),
                    }

        return asyncio.run(_run())  # type: ignore[no-any-return]

    def test_single_line_layout(self) -> None:
        s = self._scenario(0)
        self.assertEqual(s["prompt_h"], 1)  # 1 行内容 → 1 格
        self.assertEqual(s["btn_h"], 1)  # 100% 跟随，与输入框齐平
        self.assertEqual(s["btn_w"], 3)  # 图标钮窄宽
        self.assertEqual(s["btn_label"], "↑")  # 无文字，只图标
        # 上下边框 = 容器行高 1 + 两条边
        self.assertEqual(s["row_h"], 3)

    def test_grows_to_max_6(self) -> None:
        s = self._scenario(10)  # 10 次换行 → 被 max-height 截断
        self.assertEqual(s["prompt_h"], 6)
        self.assertEqual(s["btn_h"], 6)  # 按钮随行高同步拉伸

    def test_row_borders_top_bottom_only(self) -> None:
        s = self._scenario(0)
        # 上下为 tall 细线（色 = $surface），左右 bordertype 为空 = 无边框
        self.assertIn("tall", str(s["row_border_top"]))
        self.assertTrue(str(s["row_border_left"]).startswith("('',"), s["row_border_left"])


if __name__ == "__main__":
    unittest.main()
