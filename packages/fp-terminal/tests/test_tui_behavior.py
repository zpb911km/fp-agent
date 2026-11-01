"""TUI 行为测试 — n6 流式渲染 / n7 提交分流与斜杠禁用 / n8 AskModal

全部用 textual run_test pilot（headless），portal 以桩替身注入，
不连 LLM。重点锁定任务 #156 的交互契约：
- 忙碌时斜杠被拒（n3 决策的前端面）
- 忙碌时普通输入走 submit 队列且视觉回显 ⏳
- ask 选项卡返回选项原文 / 编号 / 推荐值 / Esc 空串
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import time
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from textual.widgets import Static

from fp_cli.tui import TOOL_RESULT_MAX_LINES, TUIIO, AskModal, FPApp, PromptInput, ThinkCard, ToolCard


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
        self.cancelled = False
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
        self.cancelled = True

    @property
    def transcript(self) -> list[dict[str, str]]:
        return []


async def _wait_until(pilot, predicate, *, timeout: float = 3.0, step: float = 0.02) -> bool:  # noqa: ANN001
    """等 ``predicate()`` 成立（返回 True），或超时（返回 False）。

    替代「``pause(固定时长)`` 后立即断言」：UI 反应耗时随机器负载浮动，固定短
    延迟在慢机上必然偶发假失败（CI 曾因此在 ubuntu 3.11 挂过 ``TestThinkCard``
    的点击开合）。条件等待在快机上立刻返回，慢机上最多等到 ``timeout``；若
    predicate 永不成立（真 bug）仍会返回 False 让断言失败——不掩盖问题。
    """
    deadline = time.monotonic() + timeout
    while True:
        if predicate():
            return True
        if time.monotonic() >= deadline:
            return False
        await pilot.pause(step)


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

    def test_placeholder_does_not_advertise_keys(self) -> None:
        """占位文案**不得写死按键**：换行/发送都是可改键动作，写死即谎话。

        旧文案「消息…（Enter 换行 · Shift+Enter 发送）」在用户改键后就是错的，
        而占位符恰恰是用户最先读到的一行——文案应只讲与按键无关的信息。
        """
        from fp_cli.tui import PROMPT_HINT_BUSY, PROMPT_HINT_IDLE

        for text in (PROMPT_HINT_IDLE, PROMPT_HINT_BUSY):
            with self.subTest(text=text):
                for key_word in ("Enter", "Shift", "Ctrl", "Alt"):
                    self.assertNotIn(key_word, text, f"占位文案混入了按键名：{text}")
        self.assertIn("/", PROMPT_HINT_IDLE)  # 只保留与按键无关的命令提示


class TestCopyAndFallbackKeys(unittest.TestCase):
    """复制与打断是两个**独立动作**，各占自己的键槽。

    - ``copy_selection``（默认 ``Ctrl+C``）：选区 → 复制，不退出；无选区 → 提示。
    - ``interrupt``（默认**未绑定**）：打断运行中的操作。
      不再有「有选区就复制、忙就打断」的运行时分流——那是不可见也改不了的隐式语义。
    """

    def _run(
        self,
        *,
        press: str,
        busy: bool = False,
        select: bool = False,
        initial: str = "",
        bind: dict[str, str] | None = None,
    ) -> dict[str, object]:
        from fp_core.api import portal as real_portal

        fake = _FakePortal(busy=busy)

        async def _run_app() -> dict[str, object]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            exits: list[int] = []
            app.exit = lambda *a, **k: exits.append(1)  # type: ignore[method-assign]
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
                    if select:
                        prompt.text = "hello"
                        prompt.move_cursor((0, 5))
                        await pilot.press("shift+home")  # 选中整行 hello
                    if bind:
                        # 打断默认未绑定，测它的用例自己设键
                        app.set_keymap(bind)
                        app.refresh_key_actions(bind)
                    await pilot.press(*press.split())
                    await pilot.pause(0.1)
                    return {
                        "clipboard": app.clipboard,
                        "exits": len(exits),
                        "cancelled": fake.cancelled,
                        "sent": list(fake.sent),
                        "selected": prompt.selected_text,
                    }

        return asyncio.run(_run_app())  # type: ignore[no-any-return]

    def test_ctrl_c_copies_prompt_selection(self) -> None:
        s = self._run(press="ctrl+c", select=True)
        self.assertEqual(s["clipboard"], "hello")
        self.assertEqual(s["exits"], 0)  # 复制而非退出

    def test_shift_variant_not_bound(self) -> None:
        """一个动作一个键：``ctrl+shift+c`` 不再是复制别名（只留 ``ctrl+c``）。"""
        s = self._run(press="ctrl+shift+c", select=True)
        self.assertEqual(s["clipboard"], "")
        self.assertEqual(s["exits"], 0)

    def test_ctrl_c_idle_without_selection_does_not_exit(self) -> None:
        s = self._run(press="ctrl+c")
        self.assertEqual(s["exits"], 0)  # 不再裸退出
        self.assertFalse(s["cancelled"])

    def test_ctrl_c_busy_no_longer_interrupts(self) -> None:
        """``Ctrl+C`` 只管复制：忙碌时按它不该把任务打断掉。

        旧实现让同一个键在「有选区」和「忙碌」两种情况下分流到不同语义，
        用户既看不见也无法配置。现在打断是独立动作，需要用户自己绑键。
        """
        s = self._run(busy=True, press="ctrl+c")
        self.assertFalse(s["cancelled"])
        self.assertEqual(s["exits"], 0)

    def test_interrupt_action_cancels_when_busy(self) -> None:
        """``interrupt`` 自己绑了键就能打断（默认未绑定，由用户设）。"""
        s = self._run(busy=True, bind={"interrupt": "f8"}, press="f8")
        self.assertTrue(s["cancelled"])
        self.assertEqual(s["exits"], 0)

    def test_ctrl_j_not_bound(self) -> None:
        """一个动作一个键：``ctrl+j`` 不再是发送别名（只留 ``shift+enter``）。

        终端里 ``ctrl+j`` 与 ``LF`` 同字节，落到输入框就是换行——这正是取消
        别名的理由：同一个字节不可能既换行又发送。
        """
        s = self._run(press="ctrl+j", initial="hi")
        self.assertEqual(s["sent"], [])

    def test_shift_enter_still_sends(self) -> None:
        s = self._run(press="shift+enter", initial="yo")
        self.assertEqual(s["sent"], ["yo"])

    def test_chat_drag_selection_copy(self) -> None:
        """聊天区拖选（textual 屏幕级选区）→ Ctrl+C 复制到剪贴板。"""
        from textual.events import MouseMove

        from fp_core.api import portal as real_portal

        fake = _FakePortal(busy=False)

        async def _run() -> dict[str, str]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    block = app.query_one("#chat").children[1]  # head 块（[0] 是空锚点）
                    reg = app.screen.find_widget(block).region
                    await pilot.mouse_down(widget=block, offset=(1, 0))
                    prev = reg.x + 1
                    for x in range(prev + 2, reg.x + 13, 2):
                        app.post_message(
                            MouseMove(None, x, reg.y, x - prev, 0, 1, False, False, False, screen_x=x, screen_y=reg.y)
                        )
                        prev = x
                        await pilot.pause(0.02)
                    await pilot.mouse_up(widget=block, offset=(11, 0))
                    await pilot.pause(0.05)
                    selected = app.screen.get_selected_text()
                    await pilot.press("ctrl+c")
                    await pilot.pause(0.05)
                    return {"selected": selected or "", "clipboard": app.clipboard}

        out = asyncio.run(_run())
        self.assertTrue(out["selected"])  # 拖选确实选中了聊天文本
        self.assertEqual(out["clipboard"], out["selected"])


class TestThemeSignalPersist(unittest.TestCase):
    """主题信号落盘（Ctrl+P → Theme 那条路）：内置名写回 config，自定义主题不写。

    注：Ctrl+T 现在打开的是外观设置面板（见 test_appearance_panel.py），不再走
    Textual 自带主题面板；但 App 的 theme_changed_signal 订阅仍在，负责命令面板
    与程序化改主题这两条路的落盘。
    """

    def _run(self, pick: str, *, register_custom: bool = False) -> dict[str, object]:
        from fp_core.api import portal as real_portal

        fake = _FakePortal()
        tmp = tempfile.TemporaryDirectory()
        cfg = os.path.join(tmp.name, "config.json")
        with open(cfg, "w", encoding="utf-8") as f:
            json.dump({"LLM_MODEL": "m", "tui": {"theme": ""}}, f)

        try:

            async def _scenario() -> dict[str, object]:
                io = TUIIO()
                app = FPApp(io)
                io.attach(app)
                with (
                    patch.object(type(real_portal), "is_open", property(lambda self: True)),
                    patch.object(real_portal, "run", fake, create=True),
                    patch("fp_cli.appearance.configio.get_config_dir", lambda: tmp.name),
                ):
                    async with app.run_test(size=(100, 30)) as pilot:
                        await pilot.pause()
                        # 外观动作默认未绑定；本用例测的是「程序内改主题是否落盘」，
                        # 不经键位，省掉一次多余的绑定。
                        # 命令面板那条路：直接改主题 → 触发落盘
                        if register_custom:
                            from textual.theme import Theme

                            app.register_theme(Theme(name=pick, primary="#ff0000", dark=True))
                        app.theme = pick
                        await pilot.pause()
                        await pilot.pause()  # 信号两跳投递
                        return {}

            asyncio.run(_scenario())
            with open(cfg, encoding="utf-8") as f:
                written = json.load(f)
            return {"written": written}
        finally:
            tmp.cleanup()

    def test_builtin_pick_is_persisted(self) -> None:
        written = self._run("dracula")["written"]
        self.assertEqual(written["tui"]["theme"], "dracula")  # type: ignore[index]
        self.assertEqual(written["LLM_MODEL"], "m")  # type: ignore[index]

    def test_custom_theme_is_not_persisted(self) -> None:
        # fp-custom 不是内置名 → 不落盘（它只是 config 里 palette 的派生结果）
        written = self._run("fp-custom", register_custom=True)["written"]
        self.assertEqual(written["tui"]["theme"], "")  # type: ignore[index]


class TestToolCards(unittest.TestCase):
    """工具调用/返回 → 可折叠卡片（默认折叠、点击开合、内容保形不解析标记）。"""

    def _run(self, scenario):  # noqa: ANN001, ANN202 — 桩场景
        from fp_core.api import portal as real_portal

        fake = _FakePortal()
        out: dict[str, object] = {}

        async def _wrap() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    out.update(await scenario(io, app, pilot))

        asyncio.run(_wrap())
        return out

    @staticmethod
    def _title(card):  # noqa: ANN001, ANN205 — 卡片
        from textual.widgets._collapsible import CollapsibleTitle

        return card.query_one(CollapsibleTitle).render().plain

    def test_cards_default_collapsed_and_titled(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.tool_call("bash", {"command": "ls", "timeout": 30})
            io.tool_result("out1\nout2")
            await pilot.pause(0.1)
            cards = list(app.query(ToolCard))
            return {
                "n": len(cards),
                "collapsed": [c.collapsed for c in cards],
                "titles": [self._title(c) for c in cards],
            }

        out = self._run(_scenario)
        self.assertEqual(out["n"], 2)  # 调用卡 + 返回卡
        self.assertEqual(out["collapsed"], [True, True])  # 默认折叠
        # 调用卡折叠态只露工具名，参数不外泄
        title0 = out["titles"][0]  # type: ignore[index]
        self.assertIn("bash", title0)
        self.assertNotIn("command", title0)
        self.assertIn("返回 · 2 行", out["titles"][1])  # type: ignore[operator]

    def test_call_card_body_has_full_args(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.tool_call("bash", {"command": "ls", "timeout": 30})
            await pilot.pause(0.1)
            card = app.query(ToolCard).first()
            body = card.query_one("Contents").query("Static").first().render()
            return {"body": str(body)}

        out = self._run(_scenario)
        self.assertIn('"command": "ls"', out["body"])  # type: ignore[operator]
        self.assertIn('"timeout": 30', out["body"])  # type: ignore[operator]

    def test_click_expands_card(self) -> None:
        from textual.widgets._collapsible import CollapsibleTitle

        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.tool_call("bash", {"command": "ls"})
            await pilot.pause(0.1)
            card = app.query(ToolCard).first()
            await pilot.click(card.query_one(CollapsibleTitle))
            await pilot.pause(0.05)
            return {"collapsed": card.collapsed}

        self.assertFalse(self._run(_scenario)["collapsed"])

    def test_args_brackets_render_literally(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.tool_call("grep", {"pattern": "[a-z]+"})
            await pilot.pause(0.1)
            card = app.query(ToolCard).first()
            body = card.query_one("Contents").query("Static").first().render()
            return {"body": str(body)}

        # 参数里的 [ ] 不得被当作 Textual 标记解析（rich Text 保形）
        self.assertIn("[a-z]+", self._run(_scenario)["body"])  # type: ignore[operator]

    def test_long_result_body_truncated(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.tool_result("\n".join(str(i) for i in range(TOOL_RESULT_MAX_LINES + 50)))
            await pilot.pause(0.1)
            card = app.query(ToolCard).first()
            body = card.query_one("Contents").query("Static").first().render()
            return {"text": str(body)}

        out = self._run(_scenario)
        self.assertIn("已截断", out["text"])  # type: ignore[operator]


class TestThinkCard(unittest.TestCase):
    """思考流 → ThinkCard：流式展开、正文到达折叠、字数统计、点击开合。"""

    def _run(self, scenario):  # noqa: ANN001, ANN202 — 桩场景
        from fp_core.api import portal as real_portal

        fake = _FakePortal()
        out: dict[str, object] = {}

        async def _wrap() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    out.update(await scenario(io, app, pilot))

        asyncio.run(_wrap())
        return out

    @staticmethod
    def _title(card) -> str:  # noqa: ANN001, ANN205 — 卡片
        from textual.widgets._collapsible import CollapsibleTitle

        return card.query_one(CollapsibleTitle).render().plain

    def test_streaming_expanded_then_fold_on_content(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("让我想想这个问题")
            await pilot.pause(0.1)
            card = app.query(ThinkCard).first()
            during = {"collapsed": card.collapsed, "title": self._title(card)}
            io.stream_write("# 答案")
            await pilot.pause(0.1)
            return {
                "during": during,
                "after_collapsed": card.collapsed,
                "after_title": self._title(card),
                "text": card.text,
            }

        out = self._run(_scenario)
        during = out["during"]  # type: ignore[assignment]
        self.assertFalse(during["collapsed"])  # 流式期展开
        self.assertIn("思考", during["title"])
        self.assertTrue(out["after_collapsed"])  # 正文到达 → 折叠
        self.assertIn("8 字", out["after_title"])  # 字数统计（无 emoji）
        self.assertNotIn("💭", out["after_title"])
        self.assertEqual(out["text"], "让我想想这个问题")  # 全文保留在卡体

    def test_pure_think_round_stays_expanded(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("没有正文的一轮")
            io.stream_end()
            await pilot.pause(0.1)
            card = app.query(ThinkCard).first()
            return {"collapsed": card.collapsed, "title": self._title(card)}

        out = self._run(_scenario)
        self.assertFalse(out["collapsed"])  # 纯思考轮保持展开收尾
        self.assertIn("7 字", out["title"])  # type: ignore[operator]

    def test_click_folds_and_expands(self) -> None:
        from textual.widgets._collapsible import CollapsibleTitle

        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("想")
            await _wait_until(pilot, lambda: len(app.query(ThinkCard)) == 1)
            card = app.query(ThinkCard).first()
            # 首帧布局未落定时 region 高为 0，此时点击坐标不可靠——等到有尺寸再点
            await _wait_until(pilot, lambda: card.region.height > 0)
            initial = card.collapsed
            await pilot.click(card.query_one(CollapsibleTitle))
            folded = await _wait_until(pilot, lambda: card.collapsed)
            await pilot.click(card.query_one(CollapsibleTitle))
            reopened = await _wait_until(pilot, lambda: not card.collapsed)
            return {
                "initial": initial,
                "folded": folded,
                "collapsed": card.collapsed,
                "reopened": reopened,
                "same": all(c is card for c in app.query(ThinkCard)),
            }

        out = self._run(_scenario)
        self.assertFalse(out["initial"])  # 流式期初始展开
        self.assertTrue(out["folded"])  # 点击折叠（等到生效，不等固定时长）
        self.assertFalse(out["collapsed"])  # 再点展开，全文可回看
        self.assertTrue(out["reopened"])  # 展开确实生效
        self.assertTrue(out["same"])  # 全程同一张卡：未被上下文对账重画替换

    def test_interleaved_think_opens_second_card(self) -> None:
        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("第一段思考")
            io.stream_write("中间正文")
            await pilot.pause(0.1)
            io.stream_think("第二段思考")
            await pilot.pause(0.1)
            cards = list(app.query(ThinkCard))
            return {
                "n": len(cards),
                "collapsed": [c.collapsed for c in cards],
                "texts": [c.text for c in cards],
            }

        out = self._run(_scenario)
        self.assertEqual(out["n"], 2)  # 正文后穿插思考 → 另起新卡
        self.assertEqual(out["texts"], ["第一段思考", "第二段思考"])  # type: ignore[arg-type]
        # 第一张卡已被正文折叠，第二张仍在流式展开
        self.assertEqual(out["collapsed"], [True, False])  # type: ignore[arg-type]

    def test_tool_call_folds_pure_think_card(self) -> None:
        """纯思考轮（思考 → finalize → 工具调用）：工具到达即折叠思考卡。"""

        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("先想一想")
            io.stream_end()  # 轮末：断流但不折叠（此时仍展开）
            await pilot.pause(0.1)
            card = app.query(ThinkCard).first()
            before_tool = card.collapsed
            io.tool_call("bash", {"command": "ls"})
            await pilot.pause(0.1)
            return {"before_tool": before_tool, "after_tool": card.collapsed}

        out = self._run(_scenario)
        self.assertFalse(out["before_tool"])  # 纯思考轮断流时仍展开
        self.assertTrue(out["after_tool"])  # 工具调用到达 → 自动折叠

    def test_tool_then_new_think_opens_fresh_expanded_card(self) -> None:
        """思考 → 工具 → 新一轮思考：旧卡折叠，新卡展开且独立。"""

        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("第一轮思考")
            io.stream_end()
            io.tool_call("bash", {"command": "ls"})
            await pilot.pause(0.1)
            io.stream_think("第二轮思考")
            await pilot.pause(0.1)
            cards = list(app.query(ThinkCard))
            return {
                "n": len(cards),
                "collapsed": [c.collapsed for c in cards],
                "texts": [c.text for c in cards],
            }

        out = self._run(_scenario)
        self.assertEqual(out["n"], 2)
        self.assertEqual(out["texts"], ["第一轮思考", "第二轮思考"])  # type: ignore[arg-type]
        # 旧卡被折叠（工具调用 + 新卡开启双重触发），新卡展开流式
        self.assertEqual(out["collapsed"], [True, False])  # type: ignore[arg-type]

    def test_new_round_think_folds_previous_expanded_card(self) -> None:
        """两轮连续纯思考：新思考到达时旧卡先折叠——至多一张展开的卡。"""

        async def _scenario(io, app, pilot):  # noqa: ANN001, ANN202
            io.stream_think("第一轮")
            io.stream_end()
            await pilot.pause(0.1)
            first = app.query(ThinkCard).first()
            io.stream_think("第二轮")
            await pilot.pause(0.1)
            cards = list(app.query(ThinkCard))
            return {
                "n": len(cards),
                "first_text": first.text,
                "collapsed": [c.collapsed for c in cards],
            }

        out = self._run(_scenario)
        self.assertEqual(out["n"], 2)
        self.assertTrue(out["collapsed"][0])  # type: ignore[index]  # 旧卡被折叠
        self.assertFalse(out["collapsed"][1])  # type: ignore[index]  # 新卡展开


class TestStreamFlushThrottle(unittest.TestCase):
    """流式冲刷的时间节流契约（卡顿修复的回归锚）。

    `call_later` 只是「下一 tick 回调」，**不是**节流——旧实现一 token 一帧，
    每帧一次全屏重排（O(历史块数)）+ 全屏重绘。这里锁住：同一批 token 只排
    一次冲刷、间隔未满走 `set_timer`、轮末（`stream_end`）即时收尾不丢字。
    """

    class _FakeTimer:
        """定时器桩：只记录是否被取消（立即冲刷时须取消在途定时器）。"""

        def __init__(self) -> None:
            self.stopped = False

        def stop(self) -> None:
            self.stopped = True

    class _FakeApp:
        """TUIIO 的最小宿主桩：记录调度方式与投递的消息。"""

        mounted = True

        def __init__(self) -> None:
            self.immediate = 0
            self.timers: list[float] = []
            self.messages: list[object] = []
            self.timer_objs: list[TestStreamFlushThrottle._FakeTimer] = []
            self._pending_cb: Any = None

        def call_later(self, callback: Any) -> bool:
            self.immediate += 1
            self._pending_cb = callback
            return True

        def set_timer(self, delay: float, callback: Any) -> Any:  # noqa: ANN401 — 桩
            self.timers.append(delay)
            self._pending_cb = callback
            timer = TestStreamFlushThrottle._FakeTimer()
            self.timer_objs.append(timer)
            return timer

        def post_message(self, message: Any) -> bool:
            self.messages.append(message)
            return True

        def fire(self) -> None:
            cb, self._pending_cb = self._pending_cb, None
            assert cb is not None
            cb()

        def kinds(self) -> list[str]:
            return [m.kind for m in self.messages]  # type: ignore[attr-defined]

    def test_batch_tokens_into_single_flush(self) -> None:
        app = self._FakeApp()
        io = TUIIO()
        io.attach(app)  # type: ignore[arg-type]
        for _ in range(50):
            io.stream_think("想")
        self.assertEqual(app.immediate, 1)  # 50 token 只排一次冲刷
        self.assertEqual(app.messages, [])  # 未到点：一个字都还没投
        app.fire()
        self.assertEqual(app.kinds(), ["think"])
        self.assertEqual(app.messages[0].payload, "想" * 50)  # 合批，不丢字  # type: ignore[attr-defined]

    def test_second_burst_waits_for_interval(self) -> None:
        app = self._FakeApp()
        io = TUIIO()
        io.attach(app)  # type: ignore[arg-type]
        io.stream_think("第一批")
        app.fire()  # 首次冲刷：立即（距上次已够久）
        self.assertEqual(app.kinds(), ["think"])
        io.stream_think("第二批")
        self.assertEqual(app.immediate, 1)  # 没有第二次立即冲刷
        self.assertEqual(len(app.timers), 1)  # 改走定时器，间隔为正
        self.assertGreater(app.timers[0], 0.0)
        self.assertLessEqual(app.timers[0], 0.05)
        app.fire()
        self.assertEqual(app.kinds(), ["think", "think"])

    def test_stream_end_flushes_pending_immediately(self) -> None:
        app = self._FakeApp()
        io = TUIIO()
        io.attach(app)  # type: ignore[arg-type]
        io.stream_think("尾字")
        io.stream_end()  # 轮末：残留必须当场落盘，不等定时器
        self.assertEqual(app.kinds(), ["think", "finalize"])
        self.assertEqual(app.messages[0].payload, "尾字")  # type: ignore[attr-defined]

    def test_immediate_flush_cancels_pending_timer(self) -> None:
        """在途定时器被立即冲刷取消：否则它会到点再空刷一帧。"""
        app = self._FakeApp()
        io = TUIIO()
        io.attach(app)  # type: ignore[arg-type]
        io.stream_think("甲")
        app.fire()  # 首次冲刷
        io.stream_think("乙")
        self.assertEqual(len(app.timer_objs), 1)
        io.tool_call("bash", {"command": "ls"})  # 立即冲刷（工具卡要插在流段之后）
        self.assertTrue(app.timer_objs[0].stopped)
        self.assertEqual(app.kinds(), ["think", "think", "tool"])
        self.assertEqual(app.messages[1].payload, "乙")  # type: ignore[attr-defined]


class TestStatusRefreshIdempotent(unittest.TestCase):
    """状态条刷新契约：内容没变就一个字节都不更新（idle 卡顿修复的回归锚）。

    `Static.update` 默认 `layout=True`，每次调用都会触发**全屏重排**
    （`_arrange_root` 遍历所有子件，O(历史块数)）——0.4s 定时器若无条件调用，
    空闲时也在白烧 CPU 并整屏重绘。
    """

    def _spy(self, app: FPApp) -> list[bool]:
        """给 #status 的 update 装探针，返回每次调用的 layout 参数。"""
        bar = app.query_one("#status", Static)
        calls: list[bool] = []
        original = bar.update

        def spy(content: Any = "", *, layout: bool = True) -> None:
            calls.append(layout)
            original(content, layout=layout)

        bar.update = spy  # type: ignore[method-assign]
        return calls

    def test_unchanged_status_not_redrawn(self) -> None:
        from fp_core.api import portal as real_portal

        async def _scenario() -> dict[str, object]:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", _FakePortal(busy=False), create=True),
            ):
                async with app.run_test(size=(100, 30)) as pilot:
                    await pilot.pause()
                    calls = self._spy(app)
                    app._refresh_status()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    app._refresh_status()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    await pilot.pause(0.5)  # 跨过 0.4s 定时器的一拍
                    unchanged = list(calls)
                    app._status_note = "思考中…"  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    app._refresh_status()  # noqa: SLF001  # pyright: ignore[reportPrivateUsage]
                    return {"unchanged": unchanged, "changed": list(calls)}

        out = asyncio.run(_scenario())
        self.assertEqual(out["unchanged"], [])  # 空闲多拍：零更新
        self.assertEqual(out["changed"], [False])  # 变了才更新，且不触发重排


if __name__ == "__main__":
    unittest.main()
