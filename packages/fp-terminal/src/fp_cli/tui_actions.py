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
        label: 人可读短名（帮助面板左列）。
        scope: 生效区域，见 :data:`SCOPE_LABELS`。
        default_keys: 默认键串（多键 = 等价别名）。
        description: 帮助面板右列的一行说明。
        show: 是否在帮助面板列出（``False`` = 内部/自明动作）。
    """

    id: str
    label: str
    scope: str
    default_keys: tuple[str, ...]
    description: str = ""
    show: bool = True


#: 作用域 → 中文标题（帮助面板分组顺序即此元组顺序）。
SCOPE_LABELS: tuple[tuple[str, str], ...] = (
    ("global", "全局"),
    ("chat", "消息区"),
    ("prompt", "输入区"),
    ("modal", "弹窗"),
)


ACTIONS: tuple[Action, ...] = (
    # ── 全局 ──────────────────────────────────────────────────
    Action("submit", "发送消息", "global", ("shift+enter", "ctrl+j"), "Enter 换行；此键发送"),
    Action(
        "interrupt",
        "复制/打断",
        "global",
        ("ctrl+c", "ctrl+shift+c"),
        "有选区则复制，运行中则打断",
    ),
    Action("quit", "退出", "global", ("ctrl+q",), "退出 fp"),
    Action("stats", "会话统计", "global", ("ctrl+g",), "角色分布 + 长度曲线"),
    Action("appearance", "外观设置", "global", ("ctrl+t",), "主题/配色/符号，现场写回"),
    Action("keys", "键位总览/改键", "global", ("ctrl+k", "f1"), "开关快捷键面板（也可 fp --keys）"),
    # ── 消息区：鼠标滚轮 / 点击卡片的键盘等价 ──
    Action("scroll_page_up", "上翻页", "chat", ("pageup", "ctrl+u"), "消息区向上翻一屏"),
    Action("scroll_page_down", "下翻页", "chat", ("pagedown", "ctrl+d"), "消息区向下翻一屏"),
    Action("jump_latest", "跳到最新", "chat", ("ctrl+end",), "跳到最新消息"),
    Action("card_next", "下一张卡片", "chat", ("ctrl+n",), "聚焦并滚动到下一张卡片"),
    Action("card_prev", "上一张卡片", "chat", ("ctrl+p",), "聚焦并滚动到上一张卡片"),
    Action("card_toggle", "开合当前卡片", "chat", ("ctrl+o",), "无聚焦卡片时作用于最后一张"),
    Action(
        "cards_toggle_all",
        "全部展开/折叠",
        "chat",
        ("ctrl+e",),
        "有展开的卡片则全折，否则全展",
    ),
    # ── 输入区 ────────────────────────────────────────────────
    Action("history_prev", "上一条历史", "prompt", ("up",), "首行再按上溯历史"),
    Action("history_next", "下一条历史", "prompt", ("down",), "末行再按下探历史"),
    Action("complete", "补全", "prompt", ("tab",), "命令/路径补全"),
    # ── 弹窗 ──────────────────────────────────────────────────
    Action("modal_close", "关闭", "modal", ("escape",), "", show=False),
    Action("modal_confirm", "确认", "modal", ("enter",), "", show=False),
)

#: id → Action（键位校验与帮助面板共用）。
BY_ID: dict[str, Action] = {action.id: action for action in ACTIONS}


def by_scope(scope: str) -> tuple[Action, ...]:
    """取某作用域的动作（保持 :data:`ACTIONS` 内声明顺序）。"""
    return tuple(action for action in ACTIONS if action.scope == scope)


def is_valid_key(key: str) -> bool:
    """键名是否合法（Textual 长名小写，可用 ``+`` 组合修饰键）。"""
    return bool(_KEY_RE.match(key))


def _split_value(value: object) -> tuple[str, ...] | None:
    """把配置值归一为键串元组；类型不符返回 ``None``。"""
    raw: list[object]
    if isinstance(value, str):
        raw = list(value.split(","))
    elif isinstance(value, (list, tuple)):
        raw = list(cast("list[object] | tuple[object, ...]", value))
    else:
        return None
    keys = tuple(part.strip() for part in raw if isinstance(part, str) and part.strip())
    return keys


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
        return {}, ["tui.keys 必须是对象（动作 id → 键串/键串数组）"], []
    keymap: dict[str, str] = {}
    problems: list[str] = []
    stale: list[str] = []
    for raw_id, raw_keys in cast("dict[object, object]", raw).items():
        action_id = str(raw_id)
        if action_id not in BY_ID:
            stale.append(action_id)
            continue
        keys = _split_value(raw_keys)
        if keys is None:
            problems.append(f"{action_id} 的值须为键串或键串数组，已忽略")
            continue
        if not keys:
            problems.append(f"{action_id} 的键串为空，已忽略")
            continue
        illegal = [key for key in keys if not _KEY_RE.match(key)]
        if illegal:
            problems.append(f"{action_id} 含非法键名 {illegal}，已忽略")
            continue
        keymap[action_id] = ",".join(keys)
    return keymap, problems, stale


def load_keymap() -> tuple[dict[str, str], list[str], list[str]]:
    """从 ``config.json`` 的 ``tui.keys`` 载入键位覆盖（含陈旧条目检测）。"""
    from .appearance.configio import load_tui_config

    return parse_keymap(load_tui_config().get("keys"))


def effective_keys(app: App[Any]) -> dict[str, str]:
    """动作 id → **当前生效**键串（用户覆盖优先，回落默认）。

    读 ``App._keymap`` 是因为 Textual 未暴露公开 getter；该属性在
    :meth:`App.set_keymap` 中被整体替换，语义稳定。
    """
    overrides = cast("dict[str, str]", getattr(app, "_keymap", {}) or {})
    return {action.id: overrides.get(action.id) or ",".join(action.default_keys) for action in ACTIONS}


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
            keys = overrides.get(action.id) or ",".join(action.default_keys)
            lines.append(f"  {action.label:<14} {keys:<22} {action.description}")
        lines.append("")
    return "\n".join(lines)


def check_clashes(keymap: dict[str, str]) -> list[str]:
    """检测键位冲突：同一作用域内两个动作抢同一个键。

    只比**同作用域**（跨作用域的键由焦点链决定，不算冲突）。
    自己算而不靠 Textual 的 ``handle_bindings_clash``：实测该回调只在 keymap
    内部自相冲突时触发，**不报与默认键的冲突**（用户把 keys 绑到 stats 的
    ctrl+g 时它静默通过）。

    Args:
        keymap: ``parse_keymap`` 产出的动作 id → 键串。

    Returns:
        可读问题列表，空表示无冲突。
    """
    owners: dict[str, dict[str, set[str]]] = {}
    for action in ACTIONS:
        keys = keymap.get(action.id) or ",".join(action.default_keys)
        bucket = owners.setdefault(action.scope, {})
        for key in (part.strip() for part in keys.split(",") if part.strip()):
            bucket.setdefault(key, set()).add(action.id)
    problems: list[str] = []
    for scope, bucket in owners.items():
        for key, ids in sorted(bucket.items()):
            if len(ids) > 1:
                labels = "、".join(sorted(BY_ID[i].label for i in ids))
                problems.append(f"{dict(SCOPE_LABELS).get(scope, scope)}内 {labels} 抢 {key}")
    return problems


__all__ = [
    "ACTIONS",
    "BY_ID",
    "SCOPE_LABELS",
    "Action",
    "by_scope",
    "check_clashes",
    "effective_keys",
    "format_keymap_text",
    "is_valid_key",
    "load_keymap",
    "parse_keymap",
]
