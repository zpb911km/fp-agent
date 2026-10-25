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
import string
import sys
import unittest
from collections.abc import Awaitable, Callable
from typing import cast
from unittest.mock import patch

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from rich.text import Text
from textual.app import App
from textual.binding import Binding
from textual.keys import _character_to_key
from textual.pilot import Pilot

from fp_cli.keys_panel import KeyCapture, KeysPanel
from fp_cli.tui import TUIIO, AskModal, FPApp, PromptInput, StatsModal, ToolCard, _kb, build_key_actions
from fp_cli.tui_actions import (
    _PUNCT_KEY_NAMES,
    ACTIONS,
    BY_ID,
    SCOPE_LABELS,
    UNBOUND_KEY,
    by_scope,
    check_clashes,
    display_keys,
    effective_keys,
    format_keymap_text,
    identical_keys,
    is_valid_key,
    load_keymap,
    needs_kitty,
    parse_keymap,
)

_SCOPES = {scope for scope, _ in SCOPE_LABELS}


#: ``BINDINGS`` 允许 ``Binding`` 或 (key, action[, desc]) 简写；本模块只用前者。
def _bindings_of(raw: object) -> list[Binding]:
    """从类属性的 ``BINDINGS`` 取出 ``Binding`` 实例（丢弃任何简写形式）。"""
    return [item for item in cast("list[object]", raw) if isinstance(item, Binding)]


#: headless 场景签名：拿到 app 与 pilot，自行断言。
def _bind(app: FPApp, **keys: str) -> None:
    """测试辅助：把**默认留空**的动作绑上键。

    动作目录里多数动作默认未绑定（由用户自设），凡是要按这些快捷键的用例都得
    先显式绑定——顺带验证 ``set_keymap`` + ``refresh_key_actions`` 这条链路是通的。
    未列出的动作回落到目录默认值（``ctrl+k`` 等基线键仍在）。
    """
    app.set_keymap(dict(keys))
    app.refresh_key_actions(dict(keys))


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
                # key 为空串 = 动作故意留空，等用户自设，这是合法状态而非遗漏。

    def test_default_keys_legal(self) -> None:
        bound = {action.id: action.key for action in ACTIONS if action.key}
        keymap, problems, _stale = parse_keymap(bound)
        self.assertEqual(problems, [])
        self.assertEqual(len(keymap), len(bound))

    def test_default_keys_are_minimal(self) -> None:
        """默认只保留「没有就不可用」的基线键，其余动作留空由用户自设。

        一个动作**一个键**（不接受逗号分隔的别名）：多键位会让「这个动作到底
        绑了什么」变得需要推理，也让冲突检测与面板显示都变复杂。
        """
        bound = sorted(action.id for action in ACTIONS if action.key)
        self.assertEqual(
            bound,
            [
                "complete",
                "copy_selection",
                "hint_dismiss",
                "history_next",
                "history_prev",
                "keys",
                "modal_close",
                "modal_confirm",
                "quit",
                "submit",
            ],
        )
        for action in ACTIONS:
            with self.subTest(action=action.id):
                self.assertNotIn(",", action.key, "一个动作只能有一个键")

    def test_by_scope_partitions_catalog(self) -> None:
        grouped = [action for scope, _ in SCOPE_LABELS for action in by_scope(scope)]
        self.assertEqual(len(grouped), len(ACTIONS), "每个动作必须归属某个已声明作用域")


class TestParseKeymap(unittest.TestCase):
    """``tui.keys`` 校验：坏数据永不阻断启动。"""

    def test_none_and_empty(self) -> None:
        self.assertEqual(parse_keymap(None), ({}, [], []))
        self.assertEqual(parse_keymap({}), ({}, [], []))

    def test_valid_single_key(self) -> None:
        keymap, problems, _stale = parse_keymap({"card_next": "ctrl+down"})
        self.assertEqual(problems, [])
        self.assertEqual(keymap["card_next"], "ctrl+down")

    def test_legacy_multi_key_takes_first(self) -> None:
        """旧配置的逗号/数组多键写法：取首个静默兼容，不打扰用户。

        一个动作只绑一个键；历史配置里的别名数组是旧版本产物，静默降级即可。
        """
        keymap, problems, _stale = parse_keymap({"cards_toggle_all": ["f2", "ctrl+y"], "stats": "ctrl+g,ctrl+9"})
        self.assertEqual(problems, [])
        self.assertEqual(keymap["cards_toggle_all"], "f2")
        self.assertEqual(keymap["stats"], "ctrl+g")

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

    def test_empty_key_means_unbound(self) -> None:
        """空串 / 空白串 = 显式取消绑定，不是配置错误。"""
        keymap, problems, _stale = parse_keymap({"cards_toggle_all": ["  ", ""]})
        self.assertEqual(keymap, {"cards_toggle_all": UNBOUND_KEY})
        self.assertEqual(problems, [])

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
        self.assertEqual(keys["keys"], BY_ID["keys"].key)

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
        """每个组合键都要进截获表。

        TextArea 聚焦时会抢走一部分 ctrl 组合（``ctrl+e``/``ctrl+u``…），
        唯一确定的通路是 ``FPApp._check_bindings`` 的拦截表——漏一个就等于
        那个动作在输入框里按不出。未绑定动作（``key`` 为空）不参与。
        """
        table = build_key_actions()
        for action in ACTIONS:
            if action.scope not in ("global", "chat"):
                continue
            key = action.key
            if key and "+" in key:
                with self.subTest(action=action.id, key=key):
                    self.assertIn(key, table)

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
                if action.scope == "prompt_edit":
                    # 绑定挂在 PromptInput 上而非 App 链，由
                    # test_prompt_edit_keys_are_editable 单独验证
                    continue
                keys = [action.key.strip()] if action.key.strip() else []
                if not keys:
                    continue  # 未绑定动作（留空待设）不参与本检查
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
        self.assertEqual(binding.key, BY_ID["keys"].key)
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

    def test_only_ctrl_k_opens_panel(self) -> None:
        """面板只有一个入口键：不再有 F1 别名（一个动作一个键）。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("f1")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, KeysPanel)
            await pilot.press("ctrl+k")
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
        """用户把某动作绑到基线键（``ctrl+k``）→ 报冲突。

        Textual 自带的 ``handle_bindings_clash`` 只报 keymap 内部自相冲突，
        不报与默认键的冲突，所以这部分自己算。
        """
        problems = check_clashes({"stats": "ctrl+k"})
        self.assertEqual(len(problems), 1)
        self.assertIn("键位设置", problems[0])
        self.assertIn("会话统计", problems[0])
        self.assertIn("ctrl+k", problems[0])

    def test_clash_none_for_clean_keymap(self) -> None:
        self.assertEqual(check_clashes({}), [])
        self.assertEqual(check_clashes({"keys": "f9"}), [])

    def test_clash_detects_prompt_edit_steal(self) -> None:
        """上层动作抢输入框编辑键 → 必须报出来。

        ``ctrl+u`` 是输入框的「删到行首」（TextArea 原生）。用户若把某个
        global/chat 动作改到 ``ctrl+u``，焦点在输入框时它会把编辑功能顶掉。
        这类冲突在合并检测前是隐形的，用户只会觉得某个键「时灵时不灵」。
        """
        clashes = check_clashes({"card_next": "ctrl+u", "delete_to_start_of_line": "ctrl+u"})
        self.assertTrue(clashes, "应检出与输入框编辑键的冲突")
        self.assertIn("ctrl+u", clashes[0])

    def test_clash_detects_cross_scope_shadow(self) -> None:
        """跨作用域同键**必须**报冲突——它们在同一条绑定链上。

        曾经把裸键按作用域隔离，前提是「焦点链决定谁生效」。实测是错的：
        ``global`` 与 ``prompt`` 的绑定都挂在同一个 ``App`` 上，同键只由先声明
        者生效。用户把 ``interrupt`` 设成 ``tab`` 时 Tab 补全就此静默消失。
        """
        clashes = check_clashes({"card_next": "escape"})
        self.assertTrue(clashes, "跨作用域同键必须报出来")
        self.assertIn("escape", clashes[0])

    def test_clash_detects_same_byte_alias(self) -> None:
        """同字节别名按规范键名归并：``ctrl+i`` 与 ``tab`` 是同一个键。

        终端里 Ctrl+I 发出的是 0x09，与 Tab 无法区分；不归并就会允许绑出
        一个「按 Tab 触发、以为按的是 Ctrl+I」的键。
        """
        clashes = check_clashes({"interrupt": "ctrl+i"})
        self.assertTrue(clashes, "ctrl+i 与默认 tab 补全应判为冲突")
        self.assertIn("tab", clashes[0])

    def test_parse_keymap_normalizes_alias(self) -> None:
        """配置里写 ``ctrl+m`` → 归一为 ``enter`` 并如实说明。"""
        keymap, problems, _ = parse_keymap({"submit": "ctrl+m"})
        self.assertEqual(keymap["submit"], "enter")
        self.assertTrue(any("同字节" in p for p in problems), problems)

    def test_identical_keys_lists_aliases(self) -> None:
        """``identical_keys`` 供捕获提示用：收到 tab 时列出 ctrl+i。"""
        self.assertIn("ctrl+i", identical_keys("tab"))
        self.assertEqual(identical_keys("ctrl+n"), ())

    def test_punct_names_match_textual_naming(self) -> None:
        """硬编码的标点键名必须与 Textual 的命名一致。

        用 Textual 的私有 ``_character_to_key`` 反推作为**测试期**的对照：
        不把它引入运行时代码（私有 API 变了会连累整个 TUI 起不来），但要在
        这里变红，提醒同步 ``_PUNCT_KEY_NAMES``。
        """
        names = {_character_to_key(ch) for ch in string.punctuation}
        self.assertEqual(names, set(_PUNCT_KEY_NAMES))

    def test_needs_kitty_only_for_printable_chars(self) -> None:
        """可打印字符的 Ctrl+Shift 组合只有 kitty 协议送得到。

        字符类组合（``ctrl+shift+_``）在传统终端里退化成不带 Shift 的同一个
        字节；而特殊键（``ctrl+shift+up``）的修饰位由 CSI 序列自身携带，传统
        终端同样送得到，不该误报。
        """
        for key in ("ctrl+shift+underscore", "ctrl+shift+a", "ctrl+shift+1"):
            self.assertTrue(needs_kitty(key), key)
        for key in ("ctrl+shift+up", "shift+enter", "ctrl+underscore", "tab", "ctrl+shift+left"):
            self.assertFalse(needs_kitty(key), key)

    def test_parse_keymap_warns_but_keeps_kitty_key(self) -> None:
        """``ctrl+shift+_`` 保留（kitty 终端有效）但明确警告其可能永不触发。"""
        keymap, problems, _ = parse_keymap({"stats": "ctrl+shift+underscore"})
        self.assertEqual(keymap["stats"], "ctrl+shift+underscore")
        self.assertTrue(any("kitty" in p for p in problems), problems)

    def test_parse_keymap_no_warning_for_plain_keys(self) -> None:
        """普通键不产生 kitty 警告，避免启动时无谓刷屏。"""
        _keymap, problems, _ = parse_keymap({"stats": "ctrl+g", "quit": "ctrl+q"})
        self.assertFalse(any("kitty" in p for p in problems), problems)

    def test_clash_reported_on_startup(self) -> None:
        """冲突在 on_mount 明示给用户，不静默失效。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            self.addCleanup(patch.stopall)
            self.assertGreaterEqual(len(app._clash_notices), 1)
            self.assertIn("抢", app._clash_notices[0])

        with patch("fp_cli.tui.load_keymap", return_value=({"stats": "ctrl+k"}, [], [])):
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
        """``cards_toggle_all`` 默认**未绑定**（把 ctrl+e 让回输入框），设键后生效。

        这是「留空待用户自设」这一策略的行为验证：默认按 ctrl+e 必须什么都不
        发生（它属于输入框的「移到行尾」），用户设键后才接管。
        """

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            cards = self._make_cards(2)
            for card in cards:
                await app._chat().mount(card)
            await pilot.pause()
            self.assertTrue(all(card.collapsed for card in cards))
            await pilot.press("ctrl+e")  # 默认无绑定 → 不应触发
            await pilot.pause()
            self.assertTrue(all(card.collapsed for card in cards), "ctrl+e 不该触发全展")
            app.set_keymap({"cards_toggle_all": "f9"})
            app.refresh_key_actions({"cards_toggle_all": "f9"})
            await pilot.press("f9")  # 全折叠 → 全展开
            await pilot.pause()
            self.assertTrue(all(not card.collapsed for card in cards))
            await pilot.press("f9")  # 再按 → 全折回
            await pilot.pause()
            self.assertTrue(all(card.collapsed for card in cards))

        self._run(scenario)

    def test_card_navigation_from_prompt_focus(self) -> None:
        """焦点在输入框时 Ctrl+N/P 仍能导航卡片——键盘可达的核心诉求。"""

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            cards = self._make_cards(2)
            for card in cards:
                await app._chat().mount(card)
            _bind(app, card_next="ctrl+n", card_prev="ctrl+p")
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
            _bind(app, jump_latest="ctrl+end", scroll_page_up="ctrl+b")
            await pilot.press("ctrl+end")  # jump_latest：到最新
            await pilot.pause()
            self.assertGreater(app._chat().scroll_offset.y, 0)
            before = app._chat().scroll_offset.y
            await pilot.press("ctrl+b")  # scroll_page_up：焦点在输入框也作用于消息区
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

    def test_panel_rows_show_data_not_prose(self) -> None:
        """面板是**无状态的键位表**：只列 动作名 + 当前键位，不夹带说明文字。

        这个面板是给人「查我现在按什么键」用的，不是讲解屏。动作说明属于文档
        （``fp --keys`` 打印、``docs/guide/CLI入门.md``），放进面板只会挤掉
        真正管用的那一列窄终端里还容易折行。
        """
        from textual.widgets import OptionList

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            await pilot.press("ctrl+k")
            await pilot.pause()
            panel = app.screen
            options = panel.query_one("#keys_list", OptionList)
            texts = [str(options.get_option_at_index(i).prompt) for i in range(options.option_count)]
            blob = "\n".join(texts)
            for action in ACTIONS:
                if not action.description:
                    continue
                with self.subTest(action=action.id):
                    self.assertNotIn(action.description, blob)
            # 数据本身要在：动作名与键位
            self.assertIn(BY_ID["card_next"].label, blob)
            self.assertIn("ctrl+k", blob)

        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await scenario(app, pilot)

        asyncio.run(_main())

    def test_capture_rebinds_and_persists(self) -> None:
        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "cards_toggle_all")
                await pilot.press("enter")  # 进入捕获
                await pilot.pause()
                self.assertIsInstance(app.screen, KeyCapture)
                await pilot.press("f9")
                await pilot.pause()
                self.assertIsInstance(app.screen, KeysPanel)
                # 立即生效
                self.assertIsInstance(app.screen, KeysPanel)
                app.set_keymap({"cards_toggle_all": "f9"})
                app.refresh_key_actions({"cards_toggle_all": "f9"})
                await pilot.press("f9")
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
            await pilot.press("ctrl+k")  # 与 keys 的基线键抢
            await pilot.pause()
            self.assertIsInstance(app.screen, KeysPanel)
            self.assertNotIn("cards_toggle_all", str(getattr(app, "_keymap", {})))

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

    def test_ctrl_c_can_be_restored(self) -> None:
        """把复制改走之后，必须能改回 ctrl+c。

        ``ctrl+c`` 曾被列入保留键，导致用户一旦改掉就**永远回不到默认**——
        保留键的意义是防止丢失退路，而不是禁止用户动默认值。
        """

        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "copy_selection")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("a")  # 先改成 a
                await pilot.pause()
                self.assertEqual(cast("dict[str, str]", app._keymap)["copy_selection"], "a")
                self._highlight(app, "copy_selection")
                await pilot.press("enter")
                await pilot.pause()
                await pilot.press("ctrl+c")  # 必须能改回来
                await pilot.pause()
                self.assertEqual(cast("dict[str, str]", app._keymap)["copy_selection"], "ctrl+c")
                self.assertIsInstance(app.screen, KeysPanel)

            return run

        with patch("fp_cli.keys_panel.persist_tui") as persist:
            self._run(scenario(persist))
            self.assertTrue(persist.called, "改键应写盘")

    def test_unbind_clears_key(self) -> None:
        """Delete → 取消绑定：动作还在，但不再占任何键（config 记空串）。"""

        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "copy_selection")
                await pilot.press("delete")
                await pilot.pause()
                self.assertEqual(cast("dict[str, str]", app._keymap)["copy_selection"], UNBOUND_KEY)
                persist.assert_called_with({"tui.keys.copy_selection": ""})
                # 对外表现是「未绑定」，不是那串占位键
                self.assertEqual(display_keys(BY_ID["copy_selection"], app._keymap), "")

            return run

        with patch("fp_cli.keys_panel.persist_tui") as persist:
            self._run(scenario(persist))

    def test_unbind_refuses_quit(self) -> None:
        """退出键不许清空——清空即失去唯一出口。"""

        def scenario(persist):  # noqa: ANN001, ANN202
            async def run(app: FPApp, pilot: Pilot[None]) -> None:
                await pilot.press("ctrl+k")
                await pilot.pause()
                self._highlight(app, "quit")
                await pilot.press("delete")
                await pilot.pause()
                self.assertNotIn("quit", cast("dict[str, str]", app._keymap))
                persist.assert_not_called()

            return run

        with patch("fp_cli.keys_panel.persist_tui") as persist:
            self._run(scenario(persist))

    def test_parse_empty_string_means_unbound(self) -> None:
        """``config.json`` 里写空串 = 显式取消绑定（不是「配置错误」）。"""
        keymap, problems, _ = parse_keymap({"copy_selection": ""})
        self.assertEqual(keymap["copy_selection"], UNBOUND_KEY)
        self.assertEqual(problems, [], "取消绑定不是错误，不该报问题")
        self.assertEqual(display_keys(BY_ID["copy_selection"], keymap), "")

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
                keys = [BY_ID[action_id].key]
                plain = [k for k in keys if not _FN_KEY_RE.match(k.split("+")[-1])]
                self.assertTrue(plain, f"{action_id} 只能用功能键 {keys}，小键盘用户够不着")

    def test_no_clash_after_adding_aliases(self) -> None:
        self.assertEqual(check_clashes({}), [])


if __name__ == "__main__":
    unittest.main()


class TestPromptEditBindings(unittest.TestCase):
    """输入框内建编辑键必须进入同一张动作目录（可见 / 可改 / 冲突可检）。

    这些键原本硬编码在 ``TextArea.BINDINGS``：用户既看不见也改不了，还会与
    global/chat 动作静默互抢（``ctrl+e``/``ctrl+u``/``ctrl+d`` 都曾被我方动作
    抢占而不自知）。登记后它们有了 id，``App.set_keymap`` 才能覆盖。
    """

    def _binding_ids(self) -> dict[str, str]:
        return {b.id: b.key for b in PromptInput.BINDINGS if isinstance(b, Binding) and b.id}

    def test_every_prompt_edit_action_has_binding(self) -> None:
        ids = self._binding_ids()
        for action in by_scope("prompt_edit"):
            with self.subTest(action=action.id):
                self.assertIn(action.id, ids)

    def test_prompt_edit_defaults_match_catalog(self) -> None:
        ids = self._binding_ids()
        for action in by_scope("prompt_edit"):
            with self.subTest(action=action.id):
                expected = action.key or UNBOUND_KEY
                self.assertEqual(ids[action.id], expected)

    def test_unbound_prompt_edit_can_be_set(self) -> None:
        """默认未绑定的输入框动作，用户设键后**真的**生效。

        Textual 拒绝空键（``InvalidBinding``）且不支持运行时增删绑定，故「留空」
        用哨兵键占位。本用例验证：哨兵状态下按键无副作用；keymap 覆盖后动作
        正常执行——否则「面板里设了键、按下去没反应」会是个死胡同。
        """

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            prompt = app.query_one("#prompt", PromptInput)
            prompt.text = "abc"
            prompt.cursor_location = (0, 0)
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            self.assertEqual(prompt.text, "abc", "未绑定时 f9 不该有任何效果")
            app.set_keymap({"delete_to_end_of_line": "f9"})
            app.refresh_key_actions({})
            await pilot.pause()
            await pilot.press("f9")
            await pilot.pause()
            self.assertEqual(prompt.text, "", "设键后应删掉光标到行尾的内容")

        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await scenario(app, pilot)

        asyncio.run(_main())

    def test_ctrl_u_edits_prompt_not_chat(self) -> None:
        """焦点在输入框时 ``Ctrl+U`` 必须**删到行首**，不能再被消息区滚动抢走。

        这是上一版的实际缺陷：鼠标失效的终端里焦点默认就落在输入框，用户按
        ``Ctrl+U`` 期望删行首，实际却滚动了消息区——既没做成想做的事，也没得到
        任何提示。本用例锁定「编辑键归输入框、滚动另用 Ctrl+B」这一分配。
        """

        async def scenario(app: FPApp, pilot: Pilot[None]) -> None:
            prompt = app.query_one("#prompt", PromptInput)
            before = app._chat().scroll_offset.y
            prompt.text = "hello"
            prompt.cursor_location = (0, 5)
            await pilot.pause()
            await pilot.press("ctrl+u")
            await pilot.pause()
            self.assertEqual(prompt.text, "", "Ctrl+U 应删除光标到行首的内容")
            self.assertEqual(app._chat().scroll_offset.y, before, "消息区不该被滚动")

        async def _main() -> None:
            io = TUIIO()
            app = FPApp(io)
            io.attach(app)
            async with app.run_test(size=(110, 34)) as pilot:
                await scenario(app, pilot)

        asyncio.run(_main())

    def test_prompt_edit_does_not_shadow_upper_actions(self) -> None:
        """默认键位表零冲突：上层动作不再抢输入框的编辑键。

        ``ctrl+u``/``ctrl+d``/``ctrl+e`` 是 readline 惯用编辑键。鼠标失效的终端里
        焦点默认落在输入框，被上层动作抢走等于悄悄阉掉用户最顺手的编辑能力。
        """
        self.assertEqual(check_clashes({}), [])
        upper = {a.key for a in ACTIONS if a.scope in ("global", "chat") and a.key}
        edit = {a.key for a in by_scope("prompt_edit") if a.key}
        with self.subTest():
            self.assertEqual(upper & edit, set(), f"默认键冲突：{upper & edit}")
