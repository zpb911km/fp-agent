"""外观设置面板（Ctrl+T）行为测试 — headless，不连 LLM。

锁定 #167 的交互契约：
- Ctrl+T 打开面板；五个分页都在
- 主题：↑↓ 只预览（**不写 config**）、Enter 落盘、Esc 回滚、默认行删键
- 字段：回车编辑 → 弹窗确认 → 落盘 + 即时生效；非法值被拒；留空 = 恢复默认；r 同效
- 配色：改色后注册 fp-custom 并切换
- ASCII 开关：换底表且显式符号仍优先
- 任何改动都不得动用户的 LLM 配置
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import tempfile
import unittest
from typing import Any
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(__file__))

from test_tui_behavior import _FakePortal  # noqa: E402
from textual.widgets import Input, OptionList, Static, TabbedContent  # noqa: E402

from fp_cli.appearance import (  # noqa: E402
    CUSTOM_THEME_NAME,
    AppearancePanel,
    ValuePrompt,
    builtin_theme_names,
    install_appearance,
    install_theme,
    tui_glyph,
    tui_style,
)
from fp_cli.tui import TUIIO, FPApp  # noqa: E402


def _open_panel(app: FPApp, pilot: Any) -> AppearancePanel:  # noqa: ANN401 — pilot 类型由 textual 提供
    app.action_open_appearance()
    panel = app.screen
    assert isinstance(panel, AppearancePanel), f"Ctrl+T 应打开外观面板，实际 {type(panel).__name__}"
    return panel


def _pick_dialog(app: FPApp, value: str) -> ValuePrompt:
    prompt = app.screen
    assert isinstance(prompt, ValuePrompt), f"应弹出编辑框，实际 {type(prompt).__name__}"
    prompt.query_one("#prompt_input", Input).value = value
    return prompt


class PanelCase(unittest.TestCase):
    """公共夹具：临时 config.json + 桩 portal + headless App。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = os.path.join(self._tmp.name, "config.json")
        with open(self.cfg, "w", encoding="utf-8") as f:
            json.dump({"LLM_MODEL": "m", "tui": {"theme": "nord"}}, f)

    def tearDown(self) -> None:
        self._tmp.cleanup()
        install_appearance({})  # 还原进程级单例，防污染其他用例

    def _config(self) -> dict[str, Any]:
        with open(self.cfg, encoding="utf-8") as f:
            return json.load(f)

    def _tui(self) -> dict[str, Any]:
        section = self._config().get("tui")
        return section if isinstance(section, dict) else {}

    def _run(self, scenario: Any, size: tuple[int, int] = (120, 40)) -> Any:  # noqa: ANN401 — 场景函数
        from fp_core.api import portal as real_portal

        fake = _FakePortal()

        async def _main() -> Any:  # noqa: ANN401
            io = TUIIO()
            app = FPApp(io)
            with (
                patch.object(type(real_portal), "is_open", property(lambda self: True)),
                patch.object(real_portal, "run", fake, create=True),
                patch("fp_cli.appearance.configio.get_config_dir", lambda: self._tmp.name),
            ):
                # 与 run_tui 同样的装配顺序：先主题后语义表（面板读的是同一份单例）
                install_theme(app)
                install_appearance()
                io.attach(app)
                async with app.run_test(size=size) as pilot:
                    await pilot.pause()
                    return await scenario(app, pilot)

        return asyncio.run(_main())

    @staticmethod
    def _footer(panel: AppearancePanel) -> str:
        return str(panel.query_one("#panel_footer").render())


class TestPanelEntry(PanelCase):
    def test_ctrl_t_toggles_panel(self) -> None:
        """Ctrl+T 是开关：再按一次关闭，不会把面板叠成两层。"""

        async def scenario(app: FPApp, pilot: Any) -> None:  # noqa: ANN401
            await pilot.press("ctrl+t")
            await pilot.pause()
            self.assertIsInstance(app.screen, AppearancePanel)
            await pilot.press("ctrl+t")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, AppearancePanel)
            await pilot.press("ctrl+t")
            await pilot.pause()
            self.assertIsInstance(app.screen, AppearancePanel)
            self.assertEqual(sum(isinstance(screen, AppearancePanel) for screen in app.screen_stack), 1)

        self._run(scenario)

    def test_ctrl_t_opens_panel_and_five_tabs(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:  # noqa: ANN401
            bindings = {b.key: b.action for b in FPApp.BINDINGS}
            self.assertEqual(bindings.get("ctrl+t"), "open_appearance")
            await pilot.press("ctrl+t")
            await pilot.pause()
            panel = app.screen
            self.assertIsInstance(panel, AppearancePanel)
            assert isinstance(panel, AppearancePanel)
            tabs = [pane.id for pane in panel.query("TabPane")]
            self.assertEqual(tabs, ["tab-theme", "tab-palette", "tab-styles", "tab-symbols", "tab-general"])
            await pilot.press("escape")
            await pilot.pause()
            return type(app.screen).__name__

        self.assertEqual(self._run(scenario), "FPDefaultScreen")

    def test_restart_uses_configured_theme(self) -> None:
        # 跨重启存活：config 里的 theme 在启动时生效（面板只负责写它）
        async def scenario(app: FPApp, pilot: Any) -> str:
            del pilot
            return app.theme

        self.assertEqual(self._run(scenario), "nord")


class TestThemeTab(PanelCase):
    def test_preview_does_not_write_config(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> tuple[str, str, dict[str, Any]]:
            names = builtin_theme_names()
            expected = names[names.index("nord") + 1]  # 高亮起点 = config 里的 nord
            panel = _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("down")  # 高亮移到下一个内置主题 → 实时预览
            await pilot.pause()
            self.assertIn(expected, str(panel.query_one("#panel_help").render()))
            return app.theme, expected, self._tui()

        theme, expected, tui = self._run(scenario)
        self.assertEqual(theme, expected)
        self.assertEqual(tui, {"theme": "nord"})  # 预览期一个字节都没写

    def test_enter_persists_theme(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> tuple[str, str]:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            self.assertIn("已记入 config.json", self._footer(panel))
            help_line = str(panel.query_one("#panel_help", Static).render())
            return app.theme, help_line.split(" ·")[0].split("：")[-1]

        theme, shown = self._run(scenario)
        self.assertEqual(theme, shown)
        self.assertEqual(self._tui()["theme"], theme)
        self.assertEqual(self._config()["LLM_MODEL"], "m")

    def test_escape_rolls_back_preview(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("down")
            await pilot.pause()
            self.assertNotEqual(app.theme, "nord")
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()  # 让 theme_changed_signal 的两跳投递走完
            return app.theme

        self.assertEqual(self._run(scenario), "nord")
        self.assertEqual(self._tui(), {"theme": "nord"})  # Esc 不写盘

    def test_escape_keeps_committed_theme(self) -> None:
        # 先 Enter 落盘、再预览别的、再 Esc → 回到「已确认的」而不是打开前的
        async def scenario(app: FPApp, pilot: Any) -> str:
            _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("down", "enter")
            await pilot.pause()
            committed = app.theme
            await pilot.press("down")  # 未确认的第二个预览
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            return app.theme if app.theme == committed else f"回滚错了: {app.theme}"

        committed = self._run(scenario)
        self.assertNotEqual(committed, "nord")
        self.assertEqual(self._tui()["theme"], committed)

    def test_default_row_deletes_theme_key(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("home")  # 第一行 = 默认
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            return app.theme

        self.assertEqual(self._run(scenario), "textual-dark")
        self.assertNotIn("theme", self._tui())  # 键被删掉（回落 Textual 默认）

    def test_reset_on_theme_row(self) -> None:
        # 高亮起点 = config 里的 nord（已自定义）→ r 应把它删掉
        async def scenario(app: FPApp, pilot: Any) -> tuple[str, str]:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            panel.action_reset_field()
            await pilot.pause()
            return self._footer(panel), app.theme

        footer, theme = self._run(scenario)
        self.assertIn("主题 → 默认", footer)
        self.assertEqual(theme, "textual-dark")
        self.assertNotIn("theme", self._tui())

    def test_theme_change_after_panel_still_persists(self) -> None:
        # 回归：抑制计数不能泄漏，否则关掉面板后正常换主题就不落盘了
        async def scenario(app: FPApp, pilot: Any) -> None:
            _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            await pilot.pause()
            app.theme = "dracula"  # 等价于 Ctrl+P → Theme 那条路
            await pilot.pause()
            await pilot.pause()

        self._run(scenario)
        self.assertEqual(self._tui()["theme"], "dracula")


class TestFieldTabs(PanelCase):
    def _go(self, panel: AppearancePanel, tab: str) -> None:
        panel.query_one(TabbedContent).active = tab

    def test_style_edit_persists_and_applies(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-styles")
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = logo
            await pilot.pause()
            _pick_dialog(app, "bold green")
            await pilot.press("enter")
            await pilot.pause()
            self.assertIn("已记入 config.json", self._footer(panel))
            return tui_style("logo")

        self.assertEqual(self._run(scenario), "bold green")
        self.assertEqual(self._tui()["styles"], {"logo": "bold green"})
        self.assertEqual(self._config()["LLM_MODEL"], "m")

    def test_invalid_style_is_rejected(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-styles")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            _pick_dialog(app, "not-a-style!!")
            await pilot.press("enter")
            await pilot.pause()
            return self._footer(panel)

        footer = self._run(scenario)
        self.assertIn("不是合法的", footer)
        self.assertNotIn("styles", self._tui())  # 非法值不落盘

    def test_empty_input_resets_to_default(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            _open_panel(app, pilot)
            await pilot.pause()
            # 先写一个覆盖值
            with open(self.cfg, "w", encoding="utf-8") as f:
                json.dump({"tui": {"styles": {"logo": "bold red"}}}, f)
            install_appearance()
            panel = app.screen
            assert isinstance(panel, AppearancePanel)
            self._go(panel, "tab-styles")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            _pick_dialog(app, "")  # 留空 = 恢复默认
            await pilot.press("enter")
            await pilot.pause()
            self.assertNotIn("styles", self._tui())
            return tui_style("logo")

        self.assertEqual(self._run(scenario), "bold")

    def test_r_resets_existing_override(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            with open(self.cfg, "w", encoding="utf-8") as f:
                json.dump({"tui": {"symbols": {"bot": "★"}}}, f)
            install_appearance()
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-symbols")
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = bot
            await pilot.pause()
            self.assertEqual(tui_glyph("bot"), "★")
            panel.action_reset_field()
            await pilot.pause()
            return tui_glyph("bot")

        self.assertEqual(self._run(scenario), "🤖")
        self.assertNotIn("symbols", self._tui())

    def test_r_on_default_key_is_noop(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> tuple[str, dict[str, Any]]:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-symbols")
            await pilot.pause()
            panel.action_reset_field()  # bot 本来就是默认
            await pilot.pause()
            return self._footer(panel), self._config()

        footer, config = self._run(scenario)
        self.assertIn("本来就是默认值", footer)
        self.assertEqual(config["tui"], {"theme": "nord"})  # 没写盘

    def test_symbol_edit_persists_and_esc_cancels_dialog(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-symbols")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")  # 弹窗里 Esc = 放弃编辑
            await pilot.pause()
            self.assertIs(app.screen, panel)
            await pilot.press("enter")
            await pilot.pause()
            _pick_dialog(app, "★")
            await pilot.press("enter")
            await pilot.pause()
            return tui_glyph("bot")

        self.assertEqual(self._run(scenario), "★")
        self.assertEqual(self._tui()["symbols"], {"bot": "★"})

    def test_palette_edit_registers_custom_theme(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-palette")
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = primary
            await pilot.pause()
            _pick_dialog(app, "#101020")
            await pilot.press("enter")
            await pilot.pause()
            return app.theme

        self.assertEqual(self._run(scenario), CUSTOM_THEME_NAME)
        self.assertEqual(self._tui()["palette"], {"primary": "#101020"})

    def test_palette_edit_clears_competing_theme_key(self) -> None:
        """改配色 = 切自定义主题：必须清掉 tui.theme。

        否则「theme=nord + palette=...」按准入顺序让内置主题赢，用户改完颜色
        什么都不会变——两者本就是互斥的两种玩法。第二次改色时 fp-custom 已在
        生效，不该再重复清一次（也不该再回显提示）。
        """

        async def scenario(app: FPApp, pilot: Any) -> tuple[str, str]:
            self.assertEqual(self._tui()["theme"], "nord")  # 前置：夹具带内置主题
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-palette")
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = primary
            await pilot.pause()
            _pick_dialog(app, "#101020")
            await pilot.press("enter")
            await pilot.pause()
            first = self._footer(panel)
            await pilot.press("down")  # 第二行 = 另一色
            await pilot.press("enter")
            await pilot.pause()
            _pick_dialog(app, "#203040")
            await pilot.press("enter")
            await pilot.pause()
            return first, self._footer(panel)

        first, second = self._run(scenario)
        self.assertNotIn("theme", self._tui())  # 竞争键确已清除
        self.assertIn("清掉 tui.theme", first)
        self.assertNotIn("清掉 tui.theme", second)  # 已是自定义主题 → 不重复清
        self.assertEqual(len(self._tui()["palette"]), 2)

    def test_invalid_color_is_rejected(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> str:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-palette")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            _pick_dialog(app, "not-a-color")
            await pilot.press("enter")
            await pilot.pause()
            return self._footer(panel)

        self.assertIn("不是合法的", self._run(scenario))
        self.assertNotIn("palette", self._tui())

    def test_ascii_toggle_switches_table_but_keeps_explicit_symbols(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> tuple[str, str]:
            with open(self.cfg, "w", encoding="utf-8") as f:
                json.dump({"tui": {"symbols": {"bot": "★"}}}, f)
            install_appearance()
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-general")
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = ascii 开关
            await pilot.pause()
            return tui_glyph("bot"), tui_glyph("prompt")

        bot, prompt = self._run(scenario)
        self.assertEqual(bot, "★")  # 显式覆盖优先于 ASCII 底表
        self.assertEqual(prompt, ">")
        self.assertIs(self._tui()["ascii"], True)

    def test_boolean_field_toggles(self) -> None:
        async def scenario(app: FPApp, pilot: Any) -> list[Any]:
            panel = _open_panel(app, pilot)
            await pilot.pause()
            self._go(panel, "tab-general")
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("enter")  # 再按一次 → 关
            await pilot.pause()
            return [self._tui().get("ascii")]

        # 关掉后键仍在（值为 False）——显式关闭 ≠ 删除
        self.assertEqual(self._run(scenario), [False])


class TestKeyboardOnly(PanelCase):
    """纯键盘可达性 —— 全程只按真键，不用 ``_go()`` 程序化切页。

    此前所有用例都靠 ``_go()`` 直接设 ``TabbedContent.active``，恰好绕开了
    「焦点落在 OptionList 上时，Tabs widget 自己的 left/right 绑定不在绑定链里」
    这个事实：五个分页里只有「主题」能用键盘到达，其余四个等于鼠标专属。
    这组用例让键盘本身成为被测对象。
    """

    TABS = ["tab-theme", "tab-palette", "tab-styles", "tab-symbols", "tab-general"]

    def test_arrows_cycle_all_five_tabs(self) -> None:
        """→ 能走遍五个分页并回到起点；← 反向同样通。"""

        async def scenario(app: FPApp, pilot: Any) -> tuple[list[str], list[str]]:  # noqa: ANN401
            panel = _open_panel(app, pilot)
            await pilot.pause()
            tabs = panel.query_one(TabbedContent)
            forward: list[str] = []
            for _ in range(6):  # 恰好多按一次：验证循环回起点
                forward.append(tabs.active)
                await pilot.press("right")
                await pilot.pause()
            backward: list[str] = []
            for _ in range(3):
                await pilot.press("left")
                await pilot.pause()
                backward.append(tabs.active)
            return forward, backward

        forward, backward = self._run(scenario)
        # 记录的是「每次按键前」的 active：第 6 条回到起点 = 循环成功
        self.assertEqual(forward, [*self.TABS, self.TABS[0]])
        # 接在 forward 之后时实际停在 tab-palette（forward 末次按键的结果），
        # 故 ← 走的是 palette→theme→general→symbols
        self.assertEqual(backward, ["tab-theme", "tab-general", "tab-symbols"])

    def test_arrow_lands_on_the_visible_tab_list(self) -> None:
        """切页后焦点必须落在**新页**的列表上，否则 ↑↓ 还在操作旧页。"""

        async def scenario(app: FPApp, pilot: Any) -> list[str]:  # noqa: ANN401
            panel = _open_panel(app, pilot)
            await pilot.pause()
            seen: list[str] = []
            for _ in range(4):
                await pilot.press("right")
                await pilot.pause()
                focused = app.focused
                section_id = panel.query_one(TabbedContent).active.removeprefix("tab-")
                expected = f"list-{section_id}"
                seen.append(f"{getattr(focused, 'id', None)}=={expected}")
                # ↑↓ 必须作用在当前页：高亮应能在本列表内移动（单行页如「通用」
                # 只有一项，无可移动，跳过）
                target = panel.query_one(f"#{expected}", OptionList)
                if target.option_count > 1:
                    before = target.highlighted
                    await pilot.press("down")
                    await pilot.pause()
                    self.assertNotEqual(before, target.highlighted, f"{expected} 上按 ↓ 没有移动高亮")
            return seen

        for row in self._run(scenario):
            left, right = row.split("==")
            self.assertEqual(left, right)

    def test_tab_key_moves_focus_inside_panel(self) -> None:
        """面板内 Tab 不再是死键（它曾被 App 级 priority 的 tab_complete 吞掉）。"""

        async def scenario(app: FPApp, pilot: Any) -> list[str]:  # noqa: ANN401
            _open_panel(app, pilot)
            await pilot.pause()
            seen = [type(app.focused).__name__]
            for _ in range(3):
                await pilot.press("tab")
                await pilot.pause()
                seen.append(type(app.focused).__name__)
            return seen

        seen = self._run(scenario)
        self.assertGreater(len(set(seen)), 1, f"Tab 没能移动焦点：{seen}")
        self.assertIn("ContentTabs", seen)  # 能走到分页条

    def test_prompt_input_keeps_tab_for_completion(self) -> None:
        """主屏输入框里的 Tab 仍归补全所有 —— 门控不能误伤主交互路径。"""

        async def scenario(app: FPApp, pilot: Any) -> str:  # noqa: ANN401
            await pilot.press("tab")  # 主屏（无 modal）
            await pilot.pause()
            return type(app.focused).__name__

        # 焦点停在输入框：若 Tab 被当成 focus_next，就会跑掉
        self.assertEqual(self._run(scenario), "PromptInput")

    def test_full_keyboard_edit_round_trip(self) -> None:
        """全程键盘完成一次改动：→ 到配色 → Enter → 输入值 → Enter 落盘。"""

        async def scenario(app: FPApp, pilot: Any) -> tuple[dict[str, Any], str]:  # noqa: ANN401
            panel = _open_panel(app, pilot)
            await pilot.pause()
            await pilot.press("right")  # 主题 → 配色
            await pilot.pause()
            await pilot.press("enter")  # 第一行 = 该字段
            await pilot.pause()
            self.assertIsInstance(app.screen, ValuePrompt)
            prompt = app.screen
            assert isinstance(prompt, ValuePrompt)
            prompt.query_one("#prompt_input", Input).value = "#123456"
            await pilot.press("enter")  # 确认
            await pilot.pause()
            self.assertIsInstance(app.screen, AppearancePanel)  # 回到面板而不是退出
            palette = self._tui().get("palette")
            return (palette if isinstance(palette, dict) else {}), self._footer(panel)

        palette, footer = self._run(scenario)
        self.assertIn("#123456", palette.values())
        self.assertIn("已更新", footer)

    def test_small_terminal_keeps_title_and_help_on_screen(self) -> None:
        """80x24 下不许越界：标题/帮助行被裁掉等于键盘用户失去导航提示。"""

        async def scenario(app: FPApp, pilot: Any) -> tuple[int, int, list[str]]:  # noqa: ANN401
            panel = _open_panel(app, pilot)
            await pilot.pause()
            box = panel.query_one("#panel_box")
            help_line = str(panel.query_one("#panel_help", Static).render())
            return box.region.y, box.region.bottom, [help_line]

        top, bottom, helps = self._run(scenario, size=(80, 24))
        self.assertGreaterEqual(top, 0, "面板顶部越界，标题被裁掉")
        self.assertLessEqual(bottom, 24, "面板底部越界")
        # 帮助行里必须有分页导航提示（否则「还有别的分页」这件事无从发现）
        self.assertIn("切页", helps[0])


if __name__ == "__main__":
    unittest.main()
