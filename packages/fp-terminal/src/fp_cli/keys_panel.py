"""键位设置面板 —— 捕获式改键，**键位配坏时的 bootstrap 入口**。

为什么单独一个文件、单独一个面板：

* 交互模式与外观面板不同（捕获真实按键 vs 从枚举里选值），混进
  ``AppearancePanel`` 会把 449 行的选值状态机搅乱；
* 它必须能通过 ``/keys`` 打开 —— 斜杠命令只依赖「能打字 + 回车」，
  不受任何可配置绑定影响。这样即使 ``Ctrl+T``/``F1`` 全部失效
  （终端不认该键、用户把键改到打不出来的位置上），仍有途径改回来。

改键流程：选中动作 → 按新键 → 校验键名 → 校验同作用域冲突 →
立即 ``App.set_keymap`` 生效 + 写回 ``config.json`` 的 ``tui.keys``。
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TYPE_CHECKING, Any, cast

from rich.text import Text
from textual import events, on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from .appearance.configio import UNSET, persist_tui
from .tui_actions import (
    ACTIONS,
    BY_ID,
    SCOPE_LABELS,
    UNBOUND_KEY,
    by_scope,
    canonical_key,
    check_clashes,
    identical_keys,
    is_valid_key,
)

#: 捕获态下的"退路键"。在捕获界面按它，意图几乎总是"退出捕获"而非"要绑它"，
#: 故**取消捕获**而不写盘——曾因把 ctrl+q 当成要绑的键写进配置，导致退出键丢失。
#:
#: 只放 ``ctrl+q``：它一旦被别的动作占走就**退不出程序**，无可替代。
#: ``ctrl+c`` 曾在列，但它在"复制/打断"拆分后只是「复制选区」的默认键，把它
#: 一并禁掉会让用户改走后**再也改不回来**——保留键的意义是防止丢失退路，
#: 不是禁止用户动默认值。
RESERVED_KEYS = frozenset({"ctrl+q"})

#: 必须有键的动作：清空即失去唯一出口/入口，拒绝。
REQUIRED_ACTIONS = frozenset({"quit"})
# KeyCapture 用此前缀回传"拒绝"，与普通键名区分（面板据此给出具体原因）。
RESERVED_PREFIX = "!reserved:"

if TYPE_CHECKING:
    from textual.app import App


class KeyCapture(ModalScreen[str | None]):
    """捕获单个按键 → dismiss(键名)；Esc 取消（dismiss(None)）。

    ``BINDINGS`` 刻意留空：任何一个绑定都会让该键被上层动作吃掉，
    捕获不到用户真正按下的键。
    """

    BINDINGS: list[Binding] = []

    CSS = """
    KeyCapture { align: center middle; }
    #capture_box {
        width: 48; height: 7;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #capture_title { text-style: bold; color: $accent; }
    """

    def __init__(self, label: str) -> None:
        super().__init__()
        self._label = label

    def compose(self) -> ComposeResult:
        with Container(id="capture_box"):
            yield Static(f"为「{self._label}」按新键", id="capture_title")

    def on_key(self, event: events.Key) -> None:
        event.stop()
        event.prevent_default()
        if event.key == "escape":
            self.dismiss(None)
        elif event.key in RESERVED_KEYS:
            # 退路键：用户很可能只是想退出捕获，绝不能静默绑走。
            # 曾因此把 quit 的 ctrl+q 绑到别的动作并写盘，导致退出键失效。
            self.dismiss(f"{RESERVED_PREFIX}{event.key}")
        else:
            self.dismiss(event.key)


class KeysPanel(ModalScreen[None]):
    """键位设置面板（``Ctrl+K`` 或输入 ``/keys``）。"""

    BINDINGS = [
        Binding("escape", "close_panel", "关闭", id="keys_close"),
        Binding("r", "reset_current", "恢复默认", id="keys_reset"),
        Binding("ctrl+r", "reset_all", "全部重置", id="keys_reset_all"),
        Binding("delete", "unbind", "取消绑定", id="keys_unbind"),
        Binding("backspace", "unbind", "取消绑定", id="keys_unbind_bs", show=False),
    ]

    CSS = """
    KeysPanel { align: center middle; }
    #keys_box {
        width: 92; height: 32;
        /* 小终端（80x24 及以下）不越界：固定 32 行会把标题和操作提示裁到屏幕外，
           键盘用户就找不到「怎么改键」——那正是本面板存在的理由。 */
        max-width: 100%; max-height: 100%;
        border: round $accent; background: $surface; padding: 0 1;
    }
    #keys_title { height: 1; text-style: bold; color: $accent; }
    #keys_list { height: 1fr; background: $surface; }
    #keys_footer { height: 1; }
    """

    def __init__(self) -> None:
        super().__init__()
        # 只有**用户覆盖**（config.json 的 tui.keys），不存默认值：
        # 空 dict 即「全默认」，重置一项就是把它从 dict 里删掉。
        self._overrides: dict[str, str] = {}
        #: 面板打开时展开的动作 id（保持与 ACTIONS 声明顺序一致）
        self._ids: tuple[str, ...] = tuple(action.id for action in ACTIONS)

    # ── 宿主 ────────────────────────────────────────────────

    def _host(self) -> App[Any]:
        """宿主 App —— 刻意不反向依赖 tui.py（同 AppearancePanel）。"""
        return cast("App[Any]", self.app)  # pyright: ignore[reportUnknownMemberType]

    # ── 组装 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Container(id="keys_box"):
            yield Static("键位设置", id="keys_title")
            yield OptionList(id="keys_list")
            yield Static("", id="keys_footer")

    def on_mount(self) -> None:
        self._overrides = dict(cast("dict[str, str]", getattr(self._host(), "_keymap", {}) or {}))
        self._fill()
        self.query_one("#keys_list", OptionList).focus()

    # ── 行渲染 ──────────────────────────────────────────────

    def _keys_of(self, action_id: str) -> str:
        return self._overrides.get(action_id) or BY_ID[action_id].key

    def _row(self, action_id: str) -> Text:
        action = BY_ID[action_id]
        row = Text()
        row.append(f"{action.label:<12}", style="bold")
        keys = self._keys_of(action_id)
        if keys:
            row.append(keys, style="reverse")
        else:
            row.append("（未绑定）", style="dim italic")
        return row

    def _fill(self) -> None:
        options = self.query_one("#keys_list", OptionList)
        options.clear_options()
        scope_names = dict(SCOPE_LABELS)
        for scope in scope_names:
            members = by_scope(scope)
            if not members:
                continue
            options.add_option(Option(Text(f"— {scope_names[scope]} —", style="italic dim"), disabled=True))
            for action in members:
                options.add_option(Option(self._row(action.id), id=action.id))
        if options.option_count:
            options.highlighted = 1  # 跳过首个分组标题

    def _say(self, message: str, style: str) -> None:
        self.query_one("#keys_footer", Static).update(Text(message, style=style))

    # ── 选中 → 捕获 ─────────────────────────────────────────

    @on(OptionList.OptionSelected, "#keys_list")
    def _selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        action_id = event.option.id
        if not action_id:
            return
        self._host().push_screen(
            KeyCapture(BY_ID[action_id].label),
            lambda key: self._apply_captured(action_id, key),
        )

    def _apply_captured(self, action_id: str, key: str | None) -> None:
        label = BY_ID[action_id].label
        if key is None:
            self._say("已取消，键位未改", "dim")
            return
        if key.startswith(RESERVED_PREFIX):
            reserved = key[len(RESERVED_PREFIX) :]
            self._say(f"{reserved} 为保留键，未修改", "red")
            return
        if not is_valid_key(key):
            self._say(f"不支持的键名 {key!r}，未修改", "red")
            return
        # 同字节别名归一到实际收到的键名：否则存下 ctrl+i 却在终端永远不触发。
        key = canonical_key(key)
        candidate = {**self._overrides, action_id: key}
        clashes = check_clashes(candidate)
        if clashes:
            self._say(f"未修改：{clashes[0]}", "red")
            return
        identical = identical_keys(key)
        self._overrides = candidate
        self._commit(action_id, key)
        note = f"（与 {'/'.join(identical)} 同字节，只能按 {key}）" if identical else ""
        self._say(f"「{label}」已改为 {key}{note}，立即生效", "green")

    def _commit(self, action_id: str, value: object) -> None:
        """生效（运行时 + 写回 config.json）。"""
        host = self._host()
        host.set_keymap(self._overrides)
        # 截获表按新键位重建：否则刚改的键在输入框里按下去毫无反应。
        # getattr 而非直接调用 —— 保持本模块不反向依赖 tui.FPApp（同 _host）。
        refresh = cast(
            "Callable[[dict[str, str]], None] | None",
            getattr(host, "refresh_key_actions", None),
        )
        if refresh is not None:
            refresh(self._overrides)
        persist_tui({f"tui.keys.{action_id}": value})
        self._fill()

    # ── 重置 ────────────────────────────────────────────────

    def _highlighted_action(self) -> str | None:
        options = self.query_one("#keys_list", OptionList)
        if options.highlighted is None:
            return None
        option = options.get_option_at_index(options.highlighted)
        return option.id

    def action_reset_current(self) -> None:
        """R：当前项回落默认（从 tui.keys 删掉该键）。"""
        action_id = self._highlighted_action()
        if action_id is None:
            self._say("先选中一个动作", "yellow")
            return
        self._overrides.pop(action_id, None)
        self._commit(action_id, UNSET)
        defaults = BY_ID[action_id].key
        label = BY_ID[action_id].label
        if defaults:
            self._say(f"「{label}」已恢复默认 {defaults}", "green")
        else:
            self._say(f"「{label}」已清空", "green")

    def action_unbind(self) -> None:
        """Delete / Backspace：取消当前动作的绑定（动作保留，不再占任何键）。

        与 ``R``（恢复默认）的区别：``R`` 让动作回到出厂键，本操作让它**没有键**。
        默认本就未绑定的动作两者等价。
        """
        action_id = self._highlighted_action()
        if action_id is None:
            self._say("先选中一个动作", "yellow")
            return
        label = BY_ID[action_id].label
        if action_id in REQUIRED_ACTIONS:
            self._say(f"「{label}」必须保留一个键，未修改", "red")
            return
        self._overrides[action_id] = UNBOUND_KEY
        self._commit(action_id, "")
        self._say(f"「{label}」已取消绑定", "green")

    def action_reset_all(self) -> None:
        """Ctrl+R：清空全部覆盖，回到出厂键位。"""
        if not self._overrides:
            self._say("当前没有任何自定义键位", "dim")
            return
        self._overrides = {}
        self._host().set_keymap({})
        persist_tui({"tui.keys": UNSET})
        self._fill()
        self._say("已恢复全部默认键位", "green")

    def action_close_panel(self) -> None:
        self.dismiss(None)


__all__ = ["KeyCapture", "KeysPanel"]
