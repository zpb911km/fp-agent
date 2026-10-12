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
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_cli.tui import TOOL_RESULT_MAX_LINES, TUIIO, AskModal, FPApp, PromptInput, ToolCard


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


class TestCopyAndFallbackKeys(unittest.TestCase):
    """#159 键位修复契约：
    - Ctrl+C / Ctrl+Shift+C：选区 → 复制（不退出）；空闲无选区 → 不退出；
      忙碌无选区 → 打断（旧语义保留）
    - Ctrl+J：Shift+Enter 的终端兜底（不支持 kitty 键盘协议的终端下
      Shift+Enter 与 Enter 字节不可区分，LF 全终端可区分）
    """

    def _run(
        self,
        *,
        press: str,
        busy: bool = False,
        select: bool = False,
        initial: str = "",
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

    def test_ctrl_shift_c_copies_prompt_selection(self) -> None:
        s = self._run(press="ctrl+shift+c", select=True)
        self.assertEqual(s["clipboard"], "hello")
        self.assertEqual(s["exits"], 0)

    def test_ctrl_c_idle_without_selection_does_not_exit(self) -> None:
        s = self._run(press="ctrl+c")
        self.assertEqual(s["exits"], 0)  # 不再裸退出
        self.assertFalse(s["cancelled"])

    def test_ctrl_c_busy_without_selection_cancels(self) -> None:
        s = self._run(busy=True, press="ctrl+c")
        self.assertTrue(s["cancelled"])  # 忙碌打断语义保留
        self.assertEqual(s["exits"], 0)

    def test_ctrl_j_sends(self) -> None:
        s = self._run(press="ctrl+j", initial="hi")
        self.assertEqual(s["sent"], ["hi"])

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
                        # ① Ctrl+T 绑定指向外观面板（不再是 Textual 的 change_theme）
                        bindings = {b.key: b.action for b in FPApp.BINDINGS}
                        self.assertEqual(bindings.get("ctrl+t"), "open_appearance")
                        # ② 命令面板那条路：直接改主题 → 触发落盘
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


if __name__ == "__main__":
    unittest.main()
