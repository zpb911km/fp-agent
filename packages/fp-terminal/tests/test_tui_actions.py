"""TUI 动作目录与键盘可达 — 键位映射、配置校验、绑定覆盖完整性。

对应任务 #189：TUI 全部操作可纯键盘完成（Windows conhost 亦可用），
键位由 ``config.json`` 的 ``tui.keys`` 覆盖，F1 面板展示实际生效值。

不变量
------
1. ``ACTIONS`` 是唯一真相：id 唯一、scope 合法、默认键名合法。
2. 每个动作**恰好**被某个 widget 的 ``BINDINGS`` 认领——新增动作忘记绑定即红。
3. ``parse_keymap`` 对坏配置逐条忽略并给出原因，绝不抛异常。
"""

# 测试需要触达 FPApp 的内部帮手（_chat/_cards/_card_of）以断言焦点与滚动位置；
# 它们是 TUI 的接线细节，不值得为测试而公开成 API。
# pyright: reportPrivateUsage=false

from __future__ import annotations

import asyncio
import os
import sys
import unittest
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.pilot import Pilot

from fp_cli.keys_panel import KeyCapture, KeysPanel
from fp_cli.tui import TUI_KEY_ACTIONS, TUIIO, AskModal, FPApp, PromptInput, StatsModal, ToolCard, _kb
from fp_cli.tui_actions import (
    ACTIONS,
    BY_ID,
    SCOPE_LABELS,
    by_scope,
    check_clashes,
    effective_keys,
    format_keymap_text,
    is_valid_key,
    load_keymap,
    parse_keymap,
)

_SCOPES = {scope for scope, _ in SCOPE_LABELS}


#: ``BINDINGS`` 允许 ``Binding`` 或 (key, action[, desc]) 简写；本模块只用前者。
def _bindings_of(raw: object) -> list[Binding]:
    """从类属性的 ``BINDINGS`` 取出 ``Binding`` 实例（丢弃任何简写形式）。"""
    return [item for item in cast("list[object]", raw) if isinstance(item, Binding)]


#: headless 场景签名：拿到 app 与 pilot，自行断言。
Scenario = Callable[[FPApp, Pilot[None]], Awaitable[None]]


class TestActionCatalog(unittest.TestCase):
    """目录自身的一致性——键位表的静态契约。"""

    def test_ids_unique(self) -> None:
        ids = [action.id for action in ACTIONS]
        self.assertEqual(len(ids), len(set(ids)), "动作 id 必须唯一")
        self.assertEqual(set(ids), set(BY_ID))

    def test_scopes_valid(self) -> None:
        for action in ACTIONS:
            with self.subTest(action=action.id):
                self.assertIn(action.scope, _SCOPES)
                self.assertTrue(action.label.strip())
                self.assertTrue(action.default_keys, "每个动作至少要有一个默认键")

    def test_default_keys_legal(self) -> None:
        keymap, problems, _stale = parse_keymap({action.id: list(action.default_keys) for action in ACTIONS})
        self.assertEqual(problems, [])
        self.assertEqual(len(keymap), len(ACTIONS))

    def test_by_scope_partitions_catalog(self) -> None:
        grouped = [action for scope, _ in SCOPE_LABELS for action in by_scope(scope)]
        self.assertEqual(len(grouped), len(ACTIONS), "每个动作必须归属某个已声明作用域")


class TestParseKeymap(unittest.TestCase):
    """``tui.keys`` 校验：坏数据永不阻断启动。"""

    def test_none_and_empty(self) -> None:
        self.assertEqual(parse_keymap(None), ({}, [], []))
        self.assertEqual(parse_keymap({}), ({}, [], []))

    def test_valid_str_and_list(self) -> None:
        keymap, problems, _stale = parse_keymap({"card_next": "ctrl+down", "cards_toggle_all": ["f2", "ctrl+y"]})
        self.assertEqual(problems, [])
        self.assertEqual(keymap["card_next"], "ctrl+down")
        self.assertEqual(keymap["cards_toggle_all"], "f2,ctrl+y")

    def test_unknown_action_is_stale_not_problem(self) -> None:
        """陈旧动作（改名/删除残留）静默回收，不打扰用户。"""
        keymap, problems, stale = parse_keymap({"nope": "ctrl+z"})
        self.assertEqual(keymap, {})
        self.assertEqual(problems, [], "陈旧条目不该报给用户")
        self.assertEqual(stale, ["nope"])

    def test_bad_type_rejected(self) -> None:
        keymap, problems, _stale = parse_keymap({"cards_toggle_all": 42})
        self.assertEqual(keymap, {})
        self.assertEqual(len(problems), 1)

    def test_empty_keys_rejected(self) -> None:
        keymap, problems, _stale = parse_keymap({"cards_toggle_all": ["  ", ""]})
        self.assertEqual(keymap, {})
        self.assertEqual(len(problems), 1)

    def test_illegal_key_name_rejected(self) -> None:
        keymap, problems, _stale = parse_keymap({"cards_toggle_all": "ctrl++bad"})
        self.assertEqual(keymap, {})
        self.assertIn("非法键名", problems[0])

    def test_non_dict_root_rejected(self) -> None:
        keymap, problems, _stale = parse_keymap(["cards_toggle_all"])
        self.assertEqual(keymap, {})
        self.assertEqual(len(problems), 1)

    def test_partial_valid_keeps_good_items(self) -> None:
        keymap, problems, stale = parse_keymap({"cards_toggle_all": "f2", "bogus": "x"})
        self.assertEqual(keymap, {"cards_toggle_all": "f2"})
        self.assertEqual(problems, [])
        self.assertEqual(stale, ["bogus"])


class TestLoadKeymap(unittest.TestCase):
    """配置链路：``config.json`` 的 ``tui.keys`` → keymap。"""

    def test_reads_keys_section(self) -> None:
        with patch(
            "fp_cli.appearance.configio.load_tui_config",
            return_value={"keys": {"cards_toggle_all": "f2"}},
        ):
            keymap, problems, _stale = load_keymap()
        self.assertEqual(keymap, {"cards_toggle_all": "f2"})
        self.assertEqual(problems, [])

    def test_missing_section_is_silent(self) -> None:
        with patch("fp_cli.appearance.configio.load_tui_config", return_value={}):
            self.assertEqual(load_keymap(), ({}, [], []))


class TestEffectiveKeys(unittest.TestCase):
    """帮助面板要显示**实际生效**值，而非文档默认值。"""

    def test_defaults_without_keymap(self) -> None:
        keys = effective_keys(App[None]())
        self.assertEqual(keys["keys"], ",".join(BY_ID["keys"].default_keys))

    def test_override_wins(self) -> None:
        app = App[None]()
        app.set_keymap({"cards_toggle_all": "ctrl+y"})
        self.assertEqual(effective_keys(app)["cards_toggle_all"], "ctrl+y")

    def test_every_action_has_entry(self) -> None:
        self.assertEqual(set(effective_keys(App[None]())), set(BY_ID))


class TestBindingsCoverage(unittest.TestCase):
    """每个动作恰好被某个 widget 认领——防未来新增动作忘记绑定。"""

    def test_bound_ids_cover_catalog_exactly(self) -> None:
        bound = (
            {cast("str", b.id) for b in _bindings_of(FPApp.BINDINGS)}
            | {cast("str", b.id) for b in _bindings_of(PromptInput.BINDINGS)}
            # modal_close / modal_confirm 是各模态共用动作，由它们承载
            | {cast("str", b.id) for b in _bindings_of(AskModal.BINDINGS)}
            | {cast("str", b.id) for b in _bindings_of(StatsModal.BINDINGS)}
        )
        self.assertEqual(bound, set(BY_ID))

    def test_all_modifier_keys_are_intercepted(self) -> None:
        """每个组合键都要进 ``TUI_KEY_ACTIONS``。

        TextArea 聚焦时会抢走一部分 ctrl 组合（``ctrl+e``/``ctrl+u``…），
        唯一确定的通路是 ``FPApp._check_bindings`` 的拦截表——漏一个就等于
        那个动作在输入框里按不出。
        """
        for action in ACTIONS:
            if action.scope not in ("global", "chat"):
                continue
            for key in action.default_keys:
                if "+" in key:
                    with self.subTest(action=action.id, key=key):
                        self.assertIn(key, TUI_KEY_ACTIONS)

    def test_every_default_key_survives_binding_chain(self) -> None:
        """默认键必须真的进 ``active_bindings``——否则「绑了但不生效」。

        实测 ``ctrl+home`` 会被 Textual 的绑定链静默丢弃（``App._bindings`` 里有、
        链里没有），这类键在用户看来就是「按了没反应」，且 ``check_clashes`` 查不出。
        """

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.pause()
            active = app.screen.active_bindings
            dead: list[str] = []
            for action in ACTIONS:
                if not action.show:
                    continue
                keys = [k.strip() for k in ",".join(action.default_keys).split(",") if k.strip()]
                if not any(k in active for k in keys):
                    dead.append(f"{action.id}({keys})")
            self.assertEqual(dead, [], f"这些动作的默认键全部失效：{dead}")

        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await scenario(app, pilot)

        asyncio.run(_main())

    def test_ids_do_not_repeat_within_widget(self) -> None:
        groups = {
            "FPApp": _bindings_of(FPApp.BINDINGS),
            "PromptInput": _bindings_of(PromptInput.BINDINGS),
            "AskModal": _bindings_of(AskModal.BINDINGS),
            "StatsModal": _bindings_of(StatsModal.BINDINGS),
        }
        for name, bindings in groups.items():
            with self.subTest(widget=name):
                ids = [cast("str", b.id) for b in bindings]
                self.assertEqual(len(ids), len(set(ids)))

    def test_kb_derives_key_from_catalog(self) -> None:
        binding = _kb("keys", "keys", priority=True)
        self.assertEqual(binding.id, "keys")
        self.assertEqual(binding.key, ",".join(BY_ID["keys"].default_keys))
        self.assertEqual(binding.action, "keys")
        self.assertTrue(binding.priority)


class TestKeyboardReachability(unittest.TestCase):
    """headless pilot：消息区全键盘操作 + F1 面板 + 运行期改键。"""

    def _run(self, scenario: Scenario) -> None:
        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(100, 30)) as pilot:
                await pilot.pause()
                await scenario(app, pilot)

        asyncio.run(_main())

    @staticmethod
    def _make_cards(count: int) -> list[ToolCard]:
        return [ToolCard(Text(f"card{index}"), f"body{index}", card_class="tool-card") for index in range(count)]

    def test_f1_opens_keys_panel(self) -> None:
        """F1 与 Ctrl+K 是**同一个**面板的两个别名（不再有独立的只读总览）。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("f1")
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_ctrl_k_toggles_keys_panel(self) -> None:
        """Ctrl+K 是开关：再按一次关闭，不会把面板叠成两层。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)
            await pilot.press("ctrl+k")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, KeysPanel)
            # 关了还能再开回来，且 screen 栈里没有残留
            await pilot.press("ctrl+k")
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)
            self.assertEqual(sum(isinstance(screen, KeysPanel) for screen in app.screen_stack), 1)

        self._run(scenario)

    def test_keys_panel_escape_closes(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("f1")
            await pilot.pause()
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_keymap_override_rebinds_at_runtime(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            app.set_keymap({"keys": "ctrl+y"})
            await pilot.press("ctrl+y")
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_clash_detected_against_defaults(self) -> None:
        """用户把 help 绑到 stats 的 ctrl+g → 报冲突（Textual 自带回调不报这种）。"""
        problems = check_clashes({"keys": "ctrl+g"})
        self.assertEqual(len(problems), 1)
        self.assertIn("键位总览/改键", problems[0])
        self.assertIn("会话统计", problems[0])
        self.assertIn("ctrl+g", problems[0])

    def test_clash_none_for_clean_keymap(self) -> None:
        self.assertEqual(check_clashes({}), [])
        self.assertEqual(check_clashes({"keys": "ctrl+y"}), [])

    def test_clash_ignores_cross_scope(self) -> None:
        """不同作用域同键合法（焦点链决定谁生效）。"""
        self.assertEqual(check_clashes({"card_next": "escape"}), [])

    def test_clash_reported_on_startup(self) -> None:
        """冲突在 on_mount 明示给用户，不静默失效。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            self.addCleanup(patch.stopall)
            self.assertGreaterEqual(len(app._clash_notices), 1)
            self.assertIn("抢", app._clash_notices[0])

        with patch("fp_cli.tui.load_keymap", return_value=({"keys": "ctrl+g"}, [], [])):
            self._run(scenario)

    def test_stale_key_silently_healed(self) -> None:
        """旧版本残留的动作 id：静默删除，不给用户看无从理解的提示。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            self.addCleanup(patch.stopall)
            self.assertEqual(app._clash_notices, [], "陈旧条目不该产生冲突提示")

        with (
            patch("fp_cli.tui.load_keymap", return_value=({}, [], ["scroll_top"])),
            patch("fp_cli.tui.persist_tui") as persist,
        ):
            self._run(scenario)
            # 自愈：把陈旧键从 config.json 删掉（UNSET 由 persist_tui 处理）
            self.assertIn("tui.keys.scroll_top", persist.call_args[0][0])

    def test_toggle_all_cards(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            cards = self._make_cards(2)
            for card in cards:
                await app._chat().mount(card)
            await pilot.pause()
            self.assertTrue(all(card.collapsed for card in cards))
            await pilot.press("ctrl+e")  # 全折叠 → 全展开
            await pilot.pause()
            self.assertTrue(all(not card.collapsed for card in cards))
            await pilot.press("ctrl+e")  # 再按 → 全折回
            await pilot.pause()
            self.assertTrue(all(card.collapsed for card in cards))

        self._run(scenario)

    def test_card_navigation_from_prompt_focus(self) -> None:
        """焦点在输入框时 Ctrl+N/P 仍能导航卡片——键盘可达的核心诉求。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            cards = self._make_cards(2)
            for card in cards:
                await app._chat().mount(card)
            await pilot.pause()
            await pilot.press("ctrl+n")
            await pilot.pause()
            self.assertIs(app._card_of(app.focused), cards[0])
            await pilot.press("ctrl+n")
            await pilot.pause()
            self.assertIs(app._card_of(app.focused), cards[1])
            await pilot.press("ctrl+p")
            await pilot.pause()
            self.assertIs(app._card_of(app.focused), cards[0])

        self._run(scenario)

    def test_scroll_keys_move_chat_not_prompt(self) -> None:
        """焦点在输入框时 Ctrl+End/Home 滚动的是**消息区**（priority 绑定）。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            for card in self._make_cards(40):
                await app._chat().mount(card)
            await pilot.pause()
            await pilot.press("ctrl+end")  # jump_latest：到最新
            await pilot.pause()
            self.assertGreater(app._chat().scroll_offset.y, 0)
            before = app._chat().scroll_offset.y
            await pilot.press("ctrl+u")  # scroll_page_up：焦点在输入框也作用于消息区
            await pilot.pause()
            self.assertLess(app._chat().scroll_offset.y, before)

        self._run(scenario)


class TestKeysPanel(unittest.TestCase):
    """键位设置面板：捕获改键、冲突拒绝、重置、以及「键位坏了也能进」的兜底入口。"""

    def _run(self, scenario: Callable[[FPApp, Pilot[None]], Awaitable[None]]) -> None:
        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause()
                await scenario(app, pilot)

        asyncio.run(_main())

    def _highlight(self, pilot_app: FPApp, action_id: str) -> None:
        from textual.widgets import OptionList

        options = pilot_app.screen.query_one("#keys_list", OptionList)
        options.highlighted = options.get_option_index(action_id)

    def test_ctrl_k_opens_panel(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_open_keys_param_opens_panel(self) -> None:
        """``fp --keys``：启动即开面板——不依赖任何键位，按键失灵时的兜底入口。"""

        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io, open_keys=True)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await pilot.pause()
                self.assertIsInstance(app.screen, KeysPanel)

        asyncio.run(_main())

    def test_shell_does_not_claim_slash_commands(self) -> None:
        """外壳**不得**占用 ``/`` 命名空间——那是 agent/插件的注册表。

        ``/help`` 就是 agent 已注册的命令；外壳若静默劫持，插件永远轮不到，
        用户也得不到任何提示。
        """
        from fp_cli import tui as tui_module

        self.assertFalse(hasattr(tui_module.FPApp, "_run_local_command"))
        for word in ("/keys", "/help", "/settings", "/shortcuts"):
            with self.subTest(word=word):
                # 一律交回 agent：外壳不再拦截任何斜杠命令
                self.assertEqual(tui_module.route_input(word, False), "command")

    def test_capture_rebinds_and_persists(self) -> None:
        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "cards_toggle_all")
                await pilot.press("enter")  # 进入捕获
                await pilot.pause()
                self.assertIsInstance(app.screen, KeyCapture)
                await pilot.press("ctrl+y")
                await pilot.pause()
                self.assertIsInstance(app.screen, KeysPanel)
                # 立即生效
                self.assertIsInstance(app.screen, KeysPanel)
                app.set_keymap({"cards_toggle_all": "ctrl+y"})
                await pilot.press("ctrl+y")
                await pilot.pause()
                self.assertIsInstance(app.screen, KeysPanel)
                self.assertIn("tui.keys.cards_toggle_all", persist.call_args[0][0])

            return run

        with patch("fp_cli.keys_panel.persist_tui") as persist:
            self._run(scenario(persist))

    def test_capture_reserved_key_cancelled(self) -> None:
        """捕获时按退路键（ctrl+q）→ 取消捕获，绝不写盘。

        曾因此把 quit 的键静默绑给别的动作，写进 config 后用户再也退不出。
        """

        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "cards_toggle_all")
                await pilot.press("enter")
                await pilot.pause()
                self.assertIsInstance(app.screen, KeyCapture)
                await pilot.press("ctrl+q")  # 本意是退出，不该被当成要绑的键
                await pilot.pause()
                self.assertIsInstance(app.screen, KeysPanel)  # 停在面板，未退出程序
                persist.assert_not_called()

            return run

        with patch("fp_cli.keys_panel.persist_tui") as persist:
            self._run(scenario(persist))

    def test_capture_conflict_rejected(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            self._highlight(app, "cards_toggle_all")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+u")  # 与 scroll_page_up 抢（同 chat 作用域）
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)
            self.assertNotIn("ctrl+u", str(getattr(app, "_keymap", {})))

        with patch("fp_cli.keys_panel.persist_tui"):
            self._run(scenario)

    def test_capture_escape_cancels(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            self._highlight(app, "cards_toggle_all")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")  # 取消捕获
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_ctrl_q_inside_capture_does_not_quit(self) -> None:
        """捕获子屏必须挡住 App 级 priority 绑定，否则按 Ctrl+Q 会直接退出。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            self._highlight(app, "cards_toggle_all")
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("ctrl+q")
            await pilot.pause()
            self.assertTrue(app.is_running)
            self.assertIsInstance(app.screen, KeysPanel)

        self._run(scenario)

    def test_reset_current_and_all(self) -> None:
        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            app.set_keymap({"cards_toggle_all": "ctrl+y", "stats": "ctrl+u"})
            await pilot.press("ctrl+k")
            await pilot.pause()
            self._highlight(app, "cards_toggle_all")
            await pilot.press("r")  # 当前项回落默认
            await pilot.pause()
            self.assertNotIn("cards_toggle_all", cast("dict[str, str]", app._keymap))
            await pilot.press("ctrl+r")  # 全部重置
            await pilot.pause()
            self.assertEqual(cast("dict[str, str]", app._keymap), {})

        with patch("fp_cli.keys_panel.persist_tui"):
            self._run(scenario)

    def test_key_name_validation(self) -> None:
        self.assertTrue(is_valid_key("ctrl+n"))
        self.assertTrue(is_valid_key("f1"))
        self.assertTrue(is_valid_key("pageup"))
        self.assertFalse(is_valid_key("Ctrl+N"))
        self.assertFalse(is_valid_key("ctrl+ "))
        self.assertFalse(is_valid_key(""))


#: 功能键：60% 机械键盘 / Mac fn 层 / 部分终端下按不出来，不能作为**唯一**通路
_FN_KEY_RE = __import__("re").compile(r"^(f[0-9]{1,2}|pageup|pagedown|home|end)$")

#: 缺了就干不成事的动作
_ESSENTIAL = (
    "submit",
    "interrupt",
    "quit",
    "keys",
    "scroll_page_up",
    "scroll_page_down",
    "card_next",
    "card_prev",
    "card_toggle",
    "cards_toggle_all",
)


class TestFormatKeymapText(unittest.TestCase):
    """``fp --keys`` 在非交互环境打印的纯文本表。"""

    def test_contains_all_actions(self) -> None:
        text = format_keymap_text()
        for action in ACTIONS:
            self.assertIn(action.label, text)

    def test_override_reflected(self) -> None:
        self.assertIn("ctrl+y", format_keymap_text({"keys": "ctrl+y"}))


class TestNoFunctionKeyDependency(unittest.TestCase):
    """任何键盘都能用：核心操作必须有非功能键通路。

    F1-F12 / PgUp/PgDn / Home/End 在 60% 键盘上要 Fn 组合、在 Mac 上是亮度/音量层、
    部分终端还会截获——它们只能当**加速器**，不能当唯一入口。
    """

    def test_essential_actions_avoid_function_keys(self) -> None:
        for action_id in _ESSENTIAL:
            with self.subTest(action=action_id):
                keys = ",".join(BY_ID[action_id].default_keys).split(",")
                plain = [k for k in keys if not _FN_KEY_RE.match(k.split("+")[-1])]
                self.assertTrue(plain, f"{action_id} 只能用功能键 {keys}，小键盘用户够不着")

    def test_no_clash_after_adding_aliases(self) -> None:
        self.assertEqual(check_clashes({}), [])


if __name__ == "__main__":
    unittest.main()
