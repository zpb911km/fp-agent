"""TUI 动作目录 — 键盘可达与可配置键位的**唯一真相**。

设计要点
--------
1. 每个 :class:`Action` 对应一个 ``Binding``；``Action.id`` 同时是 Textual
   keymap 的键名——``App.set_keymap`` 按 ``Binding.id`` 覆盖默认键串。
2. 三处消费同一张表，禁止另立清单：

   - ``FPApp.BINDINGS`` 从 :data:`ACTIONS` 取默认键；
   - :func:`load_keymap` 校验 ``config.json`` 的 ``tui.keys`` 覆盖；
   - ``HelpPanel``（F1）渲染**实际生效**的键位。

3. 默认键一律取**保守集**：``ctrl+字母`` / ``f1`` / ``pgup``/``pgdn``。
   这些在 Windows conhost（无 kitty 键盘协议、无 VT 鼠标上报）上同样能收到，
   因此鼠标完全失效的终端里 TUI 仍可全功能操作——这是本模块存在的理由。
   刻意避开 ``alt+方向键``（conhost 需终端配合）与 ``ctrl+shift+字母``
   （非 kitty 协议下退化为 ``ctrl+字母``）。

键位覆盖写法（``~/.config/fp/config.json``）::

    {"tui": {"keys": {"card_next": "ctrl+down", "help": "ctrl+question_mark"}}}

值是键串或键串数组；非法项**逐条忽略并给出原因**，绝不因配置错误拒绝启动。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

if TYPE_CHECKING:
    from textual.app import App

#: 键串合法性：``ctrl+n`` / ``shift+enter`` / ``pageup`` / ``f1`` / ``ctrl+shift+c``。
_KEY_RE = re.compile(r"^[a-z0-9_]+(?:\+[a-z0-9_]+)*$")


@dataclass(frozen=True, slots=True)
class Action:
    """一个用户可触发的操作。

    Attributes:
        id: 动作标识，同时用作 ``Binding.id`` 与 keymap 键名。
        label: 人可读短名（面板左列）。
        scope: 生效区域，见 :data:`SCOPE_LABELS`。
        key: 唯一的默认键串；**空串 = 未绑定**，等用户在面板里自己设。
            一个动作只绑一个键（不接受逗号分隔的多个别名）。
        description: 一行说明——只进 ``fp --keys`` 打印与文档，**不进面板**
            （面板是无状态数据表，不是讲解屏）。
        show: 是否进 Textual 自带 footer/命令面板。
    """

    id: str
    label: str
    scope: str
    key: str = ""
    description: str = ""
    show: bool = True


#: 作用域 → 中文标题（帮助面板分组顺序即此元组顺序）。
SCOPE_LABELS: tuple[tuple[str, str], ...] = (
    ("global", "全局"),
    ("chat", "消息区"),
    ("prompt", "输入区"),
    ("prompt_edit", "输入框编辑"),
    ("modal", "弹窗"),
)

#: 未绑定动作的占位键。
#:
#: Textual 的 ``Binding`` 拒绝空键（``InvalidBinding: Can not bind empty string``），
#: 又不支持运行时增删绑定（绑定链在类 MRO 上构建，实例属性不参与）。因此「留空让
#: 用户自己设」用这个**人类按不到的**组合占位：它合法、能被 keymap 覆盖，却几乎
#: 不可能被真实按键命中。用户一旦在面板里设了键，此占位即被 keymap 替换掉。
UNBOUND_KEY = "ctrl+alt+shift+f12"


#: **同一字节、终端无法区分**的键组（每组首项是实际会收到的规范键名）。
#:
#: 这不是 Textual 的 ``KEY_ALIASES``（那张表只用于显示，**不参与绑定匹配**——
#: 实测绑定 ``ctrl+i`` 后在真实终端按 Ctrl+I 永远不会触发，因为收到的是 ``tab``）。
#: 成因是终端协议本身：控制键在 xterm 编码里就是单个控制字节，多个组合共用同一个。
#:
#: 后果：把动作绑到组内任一别名，实际生效的是**首项的字节**——绑 ``ctrl+i``
#: 等于绑 ``tab``（也就等于交出 Tab 补全）。因此配置里的别名一律归一到首项，
#: 捕获时也如实告知用户收到的是哪个键。
SAME_BYTE_KEYS: tuple[tuple[str, ...], ...] = (
    ("tab", "ctrl+i"),  # 0x09
    ("newline", "ctrl+j"),  # 0x0a
    ("enter", "ctrl+m"),  # 0x0d
    ("escape", "ctrl+left_square_bracket"),  # 0x1b
    ("backspace", "ctrl+h"),  # 0x08
    ("ctrl+underscore", "ctrl+question_mark", "ctrl+slash"),  # 0x1f
)


def canonical_key(key: str) -> str:
    """把同字节别名归一到实际会收到的规范键名（不在表内则原样返回）。"""
    for group in SAME_BYTE_KEYS:
        if key in group:
            return group[0]
    return key


def identical_keys(key: str) -> tuple[str, ...]:
    """与 ``key`` 共用同一字节的其它键名（不含自身；无别名则空元组）。"""
    canonical = canonical_key(key)
    for group in SAME_BYTE_KEYS:
        if group[0] == canonical:
            return tuple(k for k in group if k != key)
    return ()


#: Textual 给 ASCII 可打印标点起的键名（Unicode 名，永不变化）。
#: 字母与数字在键串里就是单字符本身，故只需枚举标点。
_PUNCT_KEY_NAMES = frozenset(
    {
        "ampersand", "apostrophe", "asterisk", "at", "backslash",
        "circumflex_accent", "colon", "comma", "dollar_sign", "equals_sign",
        "exclamation_mark", "full_stop", "grave_accent", "greater_than_sign",
        "left_curly_bracket", "left_parenthesis", "left_square_bracket",
        "less_than_sign", "minus", "number_sign", "percent_sign", "plus",
        "question_mark", "quotation_mark", "right_curly_bracket",
        "right_parenthesis", "right_square_bracket", "semicolon", "slash",
        "tilde", "underscore", "vertical_line",
    }
)  # fmt: skip


def needs_kitty(key: str) -> bool:
    """该组合是否**只有 kitty 键盘协议**才能送达。

    传统终端里 ``Ctrl+Shift+_`` 与 ``Ctrl+_`` 发出同一个字节（``0x1F``），
    Shift 在终端层就被抹掉，应用侧无法恢复；只有 kitty 协议的 ``CSI u``
    编码才单独表达 Shift（实测 ``\\x1b[95;6u`` → ``ctrl+shift+underscore``）。

    仅对**可打印字符**成立：``ctrl+shift+up`` 之类特殊键的修饰位由 CSI 序列
    自身携带（``\\x1b[1;6A``），传统终端同样送得到。
    """
    mods, _, last = key.rpartition("+")
    if not mods:
        return False
    parts = mods.split("+")
    if not ("ctrl" in parts and "shift" in parts):
        return False
    # 单字符（字母/数字）或标点键名 = 可打印字符。
    return len(last) == 1 or last in _PUNCT_KEY_NAMES


ACTIONS: tuple[Action, ...] = (
    # ── 全局 ──────────────────────────────────────────────────
    # 只保留「没有就不可用」的基线键；其余一律留空由用户自设。
    Action("submit", "发送消息", "global", "shift+enter", "Enter 换行；此键发送"),
    Action("copy_selection", "复制选区", "global", "ctrl+c", "选中文本写入系统剪贴板（OSC52）"),
    # 打断**不预设键**：给它 Ctrl+C 就等于和复制抢同一个键槽；给别的键则是替用户拍板。
    Action("interrupt", "打断", "global", "", "中断运行中的操作：思考保留已生成内容，工具标记失败"),
    Action("quit", "退出", "global", "ctrl+q", "退出 fp"),
    Action("keys", "键位设置", "global", "ctrl+k", "开关本面板（也可 fp --keys）"),
    Action("stats", "会话统计", "global", "", "角色分布 + 长度曲线"),
    Action("appearance", "外观设置", "global", "", "主题/配色/符号，现场写回"),
    # ── 消息区：鼠标滚轮 / 点击卡片的键盘等价 ──
    Action("scroll_page_up", "上翻页", "chat", "", "消息区向上翻一屏"),
    Action("scroll_page_down", "下翻页", "chat", "", "消息区向下翻一屏"),
    Action("jump_latest", "跳到最新", "chat", "", "跳到最新消息"),
    Action("card_next", "下一张卡片", "chat", "", "聚焦并滚动到下一张卡片"),
    Action("card_prev", "上一张卡片", "chat", "", "聚焦并滚动到上一张卡片"),
    Action("card_toggle", "开合当前卡片", "chat", "", "无聚焦卡片时作用于最后一张"),
    Action("cards_toggle_all", "全部展开/折叠", "chat", "", "有展开的卡片则全折，否则全展"),
    # ── 输入区（裸键：语义由光标位置决定，取消会破坏输入框基本可用性）──
    Action("history_prev", "上一条历史", "prompt", "up", "首行再按上溯历史"),
    Action("history_next", "下一条历史", "prompt", "down", "末行再按下探历史"),
    Action("complete", "补全", "prompt", "tab", "命令/路径补全（插入当前选中项）"),
    Action("hint_dismiss", "关闭补全浮层", "prompt", "escape", "浮层打开时优先关它"),
    # ── 输入框内编辑 ──────────────────────────────────────────
    # 默认**全部留空**：不覆盖 TextArea 原生键，编辑能力一点不少（原生键照旧生效），
    # 只是不再由我们"分配"、也不与上层动作相抢。需要换键的在这里自设。
    Action("cursor_line_start", "移到行首", "prompt_edit", "", "TextArea 原生 ctrl+a / home"),
    Action("cursor_line_end", "移到行尾", "prompt_edit", "", "TextArea 原生 ctrl+e / end"),
    Action("delete_to_start_of_line", "删到行首", "prompt_edit", "", "TextArea 原生 ctrl+u"),
    Action("delete_to_end_of_line", "删到行尾", "prompt_edit", "", "TextArea 原生 ctrl+k"),
    Action("delete_right", "删除右侧字符", "prompt_edit", "", "TextArea 原生 ctrl+d / delete"),
    Action("delete_word_left", "删除左侧单词", "prompt_edit", "", "TextArea 原生 ctrl+w"),
    Action("delete_word_right", "删除右侧单词", "prompt_edit", "", "TextArea 原生 alt+delete"),
    Action("cut", "剪切", "prompt_edit", "", "TextArea 原生 ctrl+x"),
    Action("paste", "粘贴", "prompt_edit", "", "TextArea 原生 ctrl+v"),
    Action("undo", "撤销", "prompt_edit", "", "TextArea 原生 ctrl+z"),
    Action("redo", "重做", "prompt_edit", "", "TextArea 原生 ctrl+y"),
    Action("select_all", "全选", "prompt_edit", "", "TextArea 原生 f7"),
    Action("select_line", "选中当前行", "prompt_edit", "", "TextArea 原生 f6"),
    # ── 弹窗（没有就关不掉，属基线）────────────────────────────
    Action("modal_close", "关闭", "modal", "escape", "", show=False),
    Action("modal_confirm", "确认", "modal", "enter", "", show=False),
)

#: id → Action（键位校验与帮助面板共用）。
BY_ID: dict[str, Action] = {action.id: action for action in ACTIONS}


def by_scope(scope: str) -> tuple[Action, ...]:
    """取某作用域的动作（保持 :data:`ACTIONS` 内声明顺序）。"""
    return tuple(action for action in ACTIONS if action.scope == scope)


def is_valid_key(key: str) -> bool:
    """键名是否合法（Textual 长名小写，可用 ``+`` 组合修饰键）。"""
    return bool(_KEY_RE.match(key))


def _single_key(value: object) -> str | None:
    """把配置值归一为**单个**键串；类型不符返回 ``None``。

    一个动作只绑一个键。旧配置里曾允许逗号分隔/数组形式的多个别名，
    这里取**第一个**静默兼容——不值得为一条历史配置打扰用户。
    """
    if isinstance(value, str):
        candidates = value.split(",")
    elif isinstance(value, (list, tuple)):
        candidates = [v for v in cast("list[object] | tuple[object, ...]", value) if isinstance(v, str)]
    else:
        return None
    for part in candidates:
        if part.strip():
            return part.strip()
    return ""


def parse_keymap(raw: object) -> tuple[dict[str, str], list[str], list[str]]:
    """校验 ``tui.keys`` 原始值 → (Textual keymap, 问题列表, 陈旧动作 id)。

    坏项**逐条丢弃**并记录可读原因，保证一个手滑的配置项不会让 TUI 起不来。

    **未知动作不算问题**：它只可能是旧版本留下的残条（动作被改名或删除），
    属于我们自己引入的陈旧数据，不该在启动时打扰用户。这类条目回收到
    ``stale`` 列表，由调用方静默删除，实现配置自愈。

    Args:
        raw: ``config.json`` 的 ``tui.keys`` 段（任意类型，非 dict 即视为配置错）。

    Returns:
        ``(keymap, problems, stale)``：``keymap`` 可直接喂 ``App.set_keymap``；
        ``problems`` 是**需要用户处理**的配置错误；``stale`` 是待清理的陈旧动作 id。
    """
    if raw is None:
        return {}, [], []
    if not isinstance(raw, dict):
        return {}, ["tui.keys 必须是对象（动作 id → 键串）"], []
    keymap: dict[str, str] = {}
    problems: list[str] = []
    stale: list[str] = []
    for raw_id, raw_value in cast("dict[object, object]", raw).items():
        action_id = str(raw_id)
        if action_id not in BY_ID:
            stale.append(action_id)
            continue
        key = _single_key(raw_value)
        if key is None:
            problems.append(f"{action_id} 的值须为键串，已忽略")
            continue
        if not key:
            # 空串 = 显式**取消绑定**（动作还在，但没有键）。写入占位键，
            # Textual 的 Binding 不接受空串，只能这样表达"无键"。
            keymap[action_id] = UNBOUND_KEY
            continue
        if not _KEY_RE.match(key):
            problems.append(f"{action_id} 含非法键名 {key!r}，已忽略")
            continue
        if (canonical := canonical_key(key)) != key:
            # ctrl+i 之类在终端里发不出独立字节，绑它等于绑 tab。如实告知，
            # 不静默换成别的键——用户按 Tab 却触发自己以为是 Ctrl+I 的动作最糟。
            problems.append(f"{action_id}: {key} 与 {canonical} 同字节，只能按 {canonical} 生效")
            key = canonical
        if needs_kitty(key):
            # 保留该键（kitty 终端上有效），但必须警告：传统终端下它退化成
            # 去掉 Shift 的同一个键，于是这条绑定永远等不到自己的字节。
            problems.append(f"{action_id}: {key} 需终端支持 kitty 键盘协议；否则 Shift 会被终端丢弃，该键永不触发")
        keymap[action_id] = key
    return keymap, problems, stale


def load_keymap() -> tuple[dict[str, str], list[str], list[str]]:
    """从 ``config.json`` 的 ``tui.keys`` 载入键位覆盖（含陈旧条目检测）。"""
    from .appearance.configio import load_tui_config

    return parse_keymap(load_tui_config().get("keys"))


def bound_key(key: str) -> str:
    """对外表现的生效键：占位键（显式未绑定）归一为空串。"""
    return "" if key == UNBOUND_KEY else key


def effective_keys(app: App[Any]) -> dict[str, str]:
    """动作 id → **当前生效**键串（用户覆盖优先，回落默认）。

    读 ``App._keymap`` 是因为 Textual 未暴露公开 getter；该属性在
    :meth:`App.set_keymap` 中被整体替换，语义稳定。
    """
    overrides = cast("dict[str, str]", getattr(app, "_keymap", {}) or {})
    return {action.id: bound_key(overrides.get(action.id) or action.key) for action in ACTIONS}


def display_keys(action: Action, overrides: dict[str, str] | None = None) -> str:
    """动作的**可读**生效键串；未绑定时返回空串（调用方据此显示「未绑定」）。"""
    if overrides and (override := overrides.get(action.id)):
        return bound_key(override)
    return bound_key(action.key)


def format_keymap_text(keymap: dict[str, str] | None = None) -> str:
    """键位表纯文本（不依赖 TUI：``fp --keys`` 在非交互环境下直接打印）。

    Args:
        keymap: 用户覆盖（动作 id → 键串），``None`` 视作全默认。
    """
    overrides = keymap or {}
    scope_names = dict(SCOPE_LABELS)
    lines = ["FP 快捷键（config.json 的 tui.keys 可覆盖）", ""]
    for scope, _ in SCOPE_LABELS:
        members = [action for action in ACTIONS if action.scope == scope]
        if not members:
            continue
        lines.append(f"[{scope_names[scope]}]")
        for action in members:
            keys = display_keys(action, overrides) or "（未绑定）"
            lines.append(f"  {action.label:<14} {keys:<22} {action.description}")
        lines.append("")
    return "\n".join(lines)


def check_clashes(keymap: dict[str, str]) -> list[str]:
    """检测键位冲突。

    **所有非 modal 动作共用一条绑定链**，故一律合并检测。裸键（``up``/``tab``/
    ``escape`` 之类）曾经按作用域隔离，那是错的：``global`` 与 ``prompt`` 的绑定
    都挂在同一个 ``App`` 上，同键只由先声明者生效、后声明者静默失效——用户把
    ``interrupt`` 设成 ``tab`` 时 Tab 补全就此消失，而面板一声不响。
    ``prompt_edit`` 挂在输入框上，但同键同样被上层 priority 绑定抢走，行为一致。
    ``modal`` 在自己的屏幕里，独立。

    同字节别名（``ctrl+i`` ≡ ``tab``）按规范键名归并——终端根本发不出区别。

    自己算而不靠 Textual 的 ``handle_bindings_clash``：实测该回调只在 keymap
    内部自相冲突时触发，**不报与默认键的冲突**（用户把 keys 绑到 stats 的
    ctrl+g 时它静默通过）。

    Args:
        keymap: ``parse_keymap`` 产出的动作 id → 键串。

    Returns:
        可读问题列表，空表示无冲突。
    """
    owners: dict[tuple[str, str], set[str]] = {}
    for action in ACTIONS:
        key = display_keys(action, keymap).strip()
        if not key:
            continue
        canonical = canonical_key(key)
        bucket = ("modal", canonical) if action.scope == "modal" else ("all", canonical)
        owners.setdefault(bucket, set()).add(action.id)
    problems: list[str] = []
    for (where, key), ids in sorted(owners.items()):
        if len(ids) < 2:
            continue
        label = "输入框聚焦时" if where == "all" else "弹窗内"
        labels = "、".join(sorted(BY_ID[i].label for i in ids))
        problems.append(f"{label} {labels} 抢 {key}")

    return problems


__all__ = [
    "ACTIONS",
    "BY_ID",
    "SAME_BYTE_KEYS",
    "SCOPE_LABELS",
    "UNBOUND_KEY",
    "Action",
    "bound_key",
    "by_scope",
    "canonical_key",
    "check_clashes",
    "display_keys",
    "effective_keys",
    "format_keymap_text",
    "identical_keys",
    "is_valid_key",
    "load_keymap",
    "needs_kitty",
    "parse_keymap",
]
