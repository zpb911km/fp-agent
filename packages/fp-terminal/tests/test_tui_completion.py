"""斜杠补全浮层 — 候选选择（↑↓）、帮助栏、Tab 插入选中项、Esc 关闭。

全部 headless（textual run_test pilot），portal 用桩替身注入，
命令词表取真实注册表（``fp_core.commands.get_all_commands``），
但注册表入口 ``portal.run`` 换成桩——不连实例、不连 LLM。

锁定契约：
- 输入 '/' → 浮层可见，左候选窗 + 右帮助栏；
- ↑↓ 只挪选择（不翻历史、不动正文），选中行反显；
- Tab 插入**当前选中**项（而非第一条）；
- Esc 关浮层但不清正文，继续输入自动重开；
- 帮助栏按栏宽自动折行（窄屏上下叠放），不因栏窄而折行丢失。
"""

from __future__ import annotations

import os
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from textual.geometry import Region
from textual.widgets import Static

from fp_cli import completion
from fp_cli.completion import Candidate, slash_candidates, slash_matches
from fp_cli.tui import TUIIO, FPApp, PromptInput
from fp_core.api import portal as real_portal


def _fake_run() -> SimpleNamespace:
    """portal.run 桩：命令目录取真实快照（在打桩**之前**求值），不连实例。"""
    return SimpleNamespace(command_catalog=real_portal.run.command_catalog)


def _render_lines(widget: Static) -> list[str]:
    """把 widget 的渲染结果拍平成文本行——用来断言「内容全部落在可见区」。"""
    region = widget.region
    return [
        "".join(seg.text for seg in line).rstrip()
        for line in widget.render_lines(Region(0, 0, region.width, region.height))
    ]


class TestCompletionPure(unittest.TestCase):
    """completion 纯函数层（不经 TUI）。"""

    def test_prefix_filter_and_detail(self) -> None:
        with patch.object(real_portal, "run", _fake_run(), create=True):
            cands = slash_candidates("/h")
        words = [c.word for c in cands]
        self.assertIn("/help", words)
        help_cand = next(c for c in cands if c.word == "/help")
        self.assertTrue(help_cand.desc, "/help 应带一行描述")
        self.assertIn("?", help_cand.aliases, "/help 的别名 '?' 应取自命令模块")

    def test_non_slash_returns_empty(self) -> None:
        with patch.object(real_portal, "run", _fake_run(), create=True):
            self.assertEqual(slash_candidates("hello"), [])

    def test_slash_matches_keeps_tuple_shape(self) -> None:
        # 旧签名兼容：下游按 (word, desc) 二元组解包
        with patch.object(real_portal, "run", _fake_run(), create=True):
            pairs = slash_matches("/he")
        self.assertTrue(all(isinstance(w, str) and isinstance(d, str) for w, d in pairs))

    def test_registry_failure_degrades_to_empty(self) -> None:
        with patch.object(real_portal, "run", None, create=True):
            self.assertEqual(slash_candidates("/"), [])


class TestHintPopup(unittest.TestCase):
    """TUI 侧：浮层内容与键位分流。"""

    def _run(self, scenario, *, size=(100, 30)):  # noqa: ANN001, ANN202 — 测试辅助
        async def _scenario() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            with patch.object(real_portal, "run", _fake_run(), create=True):
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    await scenario(app, pilot)

        import asyncio

        asyncio.run(_scenario())

    def _prompt(self, app: FPApp) -> PromptInput:
        return app.query_one("#prompt", PromptInput)

    def test_popup_opens_and_shows_help(self) -> None:
        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/h"
            await pilot.pause()
            hint = app.query_one("#hint")
            self.assertTrue(hint.has_class("visible"), "有候选 → 浮层应可见")
            list_text = app.query_one("#hint_list", Static)
            detail = app.query_one("#hint_detail", Static)
            self.assertIn("/help", str(list_text.render()))
            self.assertIn("/help", str(detail.render()), "帮助栏应描述当前选中项")
            self.assertIn("1/", str(detail.render()), "帮助栏应带位置计数")

    def test_arrows_move_selection_only(self) -> None:
        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/"
            await pilot.pause()
            total = len(app._hint_matches)  # noqa: SLF001 — 断言内部状态
            self.assertGreater(total, 1, "测试前提：至少两条候选")
            hist_idx = app._hist_idx  # noqa: SLF001
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app._hint_idx, 1)  # noqa: SLF001
            self.assertEqual(self._prompt(app).text, "/", "↑↓ 不该改正文")
            self.assertEqual(app._hist_idx, hist_idx)  # noqa: SLF001
            await pilot.press("down")
            await pilot.press("down")  # 越过末条 → 夹在边界，不回落历史
            await pilot.pause()
            self.assertEqual(app._hint_idx, min(2, total - 1))  # noqa: SLF001
            self.assertEqual(self._prompt(app).text, "/")

    def test_tab_inserts_selected(self) -> None:
        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/"
            await pilot.pause()
            second = app._hint_matches[1].word  # noqa: SLF001
            await pilot.press("down")
            await pilot.press("tab")
            await pilot.pause()
            self.assertEqual(self._prompt(app).text, second + " ")
            self.assertFalse(app.query_one("#hint").has_class("visible"), "插入后应收起")

    def test_escape_dismisses_then_typing_reopens(self) -> None:
        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/h"
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertFalse(app.query_one("#hint").has_class("visible"))
            self.assertEqual(self._prompt(app).text, "/h", "Esc 只关浮层，不清正文")
            self._prompt(app).text = "/he"
            await pilot.pause()
            self.assertTrue(app.query_one("#hint").has_class("visible"), "继续输入应重开")

    def test_resize_rewraps_with_new_width(self) -> None:
        """窗口变窄 → 按新宽度重折行（Resize 到达时 App.size 还是旧值，别用它）。"""

        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/"
            await pilot.pause()
            self.assertFalse(app.query_one("#hint").has_class("narrow"), "宽屏应是左右两栏")
            await pilot.resize_terminal(34, 20)
            await pilot.pause()
            hint = app.query_one("#hint")
            detail = app.query_one("#hint_detail", Static)
            self.assertTrue(hint.has_class("narrow"), "窄屏应切上下叠放")
            self.assertEqual(len(_render_lines(detail)), detail.region.height, "重折行后帮助栏不该被裁")

        self._run(scenario, size=(100, 30))

    def test_help_shows_usage_block(self) -> None:
        """帮助栏抽的是「用法:」小节（用户面），不是 docstring 里的开发者注记。"""

        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            self._prompt(app).text = "/resume"
            await pilot.pause()
            text = "\n".join(_render_lines(app.query_one("#hint_detail", Static)))
            self.assertIn("/resume latest", text, "用法正文应进帮助栏")
            self.assertNotIn("不再经过 Agent 中转", text, "开发者注记不该进帮助栏")

        self._run(scenario)

    def test_long_help_wraps_and_is_not_clipped(self) -> None:
        """宽屏两栏 / 窄屏叠放：都要折行显示，且**不许**超出浮层被裁。"""
        long_desc = "这是一个很长的命令描述，用来验证帮助栏会按实际栏宽自动折行而不是被裁掉"
        cands = [
            Candidate(f"/cmd{i}", long_desc, (), "第一段帮助正文，同样需要折行。\n第二段帮助正文，同样需要折行显示。")
            for i in range(5)
        ]

        for size, expect_narrow in (((100, 30), False), ((34, 22), True)):
            with self.subTest(size=size):
                self._run(self._wrap_check(cands, expect_narrow), size=size)

    def _wrap_check(self, cands: list[Candidate], expect_narrow: bool):  # noqa: ANN202 — 测试场景工厂
        async def scenario(app: FPApp, pilot) -> None:  # noqa: ANN001
            with patch.object(completion, "slash_candidates", return_value=cands):
                self._prompt(app).text = "/cmd"
                await pilot.pause()
            hint = app.query_one("#hint")
            detail = app.query_one("#hint_detail", Static)
            self.assertEqual(hint.has_class("narrow"), expect_narrow)
            lines = _render_lines(detail)
            # 行数 == 区域高度 = 内容全部落在可见区（旧实现折成 11 行只给 5 行高）
            self.assertEqual(len(lines), detail.region.height, f"帮助栏被裁：{lines}")
            self.assertGreater(len(lines), 3, "多行信息应真的占了多行")
            self.assertIn("Esc", lines[-1], "键位提示行必须可见")
            self.assertLessEqual(hint.region.height, 13, "浮层总高要有上限")

        return scenario


if __name__ == "__main__":
    unittest.main()
