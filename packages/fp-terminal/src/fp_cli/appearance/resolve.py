"""语义键 → 渲染层取值：``Appearance`` 快照 + 进程级单例。

渲染调用点只认两个函数（tui.py / display.py / main.py / cli_io.py）::

    tui_style("info")   → "cyan"        # rich 样式串
    tui_glyph("bot")    → "🤖"          # 符号字形

它们读的是模块级单例 ``_appearance``（:func:`install_appearance` 装配）。
装配发生在两个地方：``run_tui()``（TUI 路径）与 ``main.py`` 的 REPL 路径——
**漏了后一处，REPL 的符号会静默用出厂默认（配置不生效）**。

回落（坏数据静默降级，本模块不抛异常）：
    样式：tui.styles[key] > DEFAULT_STYLES（键白名单，未知键忽略）
    符号：tui.symbols[key] > 底表（ascii=true 时换 ASCII_SYMBOLS）
"""

from __future__ import annotations

from typing import Any, cast

from .configio import load_config
from .schema import ASCII_SYMBOLS, DEFAULT_STYLES, DEFAULT_SYMBOLS
from .validate import valid_style

#: is_custom 的「键不存在」哨兵（区别于值为 None）
_MISSING = object()


class Appearance:
    """解析后的外观快照（不可变语义，只读访问）。

    ``style()``/``glyph()``：显式配置 > 底表（ascii 时符号换 ASCII 表）> 出厂默认。
    未知键返回出厂默认的对应值（无则空串）——调用方笔误不会崩，只会回到默认。

    ``tui`` / ``is_custom()`` 供**设置面板**用：面板要显示当前值、默认值与
    「哪些键被改过」，这些都需要原始 tui 段，而渲染层不需要。
    """

    __slots__ = ("_styles", "_symbols", "_tui")

    def __init__(
        self,
        styles: dict[str, str],
        symbols: dict[str, str],
        tui: dict[str, Any] | None = None,
    ) -> None:
        self._styles = styles
        self._symbols = symbols
        self._tui = tui or {}

    def style(self, key: str) -> str:
        return self._styles.get(key, DEFAULT_STYLES.get(key, ""))

    def glyph(self, key: str) -> str:
        return self._symbols.get(key, DEFAULT_SYMBOLS.get(key, ""))

    @property
    def tui(self) -> dict[str, Any]:
        """config.json 的原始 ``tui`` 段（面板读 palette / theme / ascii 用）。"""
        return self._tui

    def is_custom(self, dotted: str) -> bool:
        """该点路径（如 ``tui.styles.info``）在 config 里有显式值吗？"""
        parts = [p for p in dotted.split(".") if p]
        if len(parts) < 2 or parts[0] != "tui":
            return False
        node: object = self._tui
        for part in parts[1:]:
            if not isinstance(node, dict):
                return False
            value = cast(dict[str, object], node).get(part, _MISSING)
            if value is _MISSING:
                return False
            node = value
        return True

    def __eq__(self, other: object) -> bool:
        """值相等：用于断言「某份配置是否等价于无配置」（只看解析结果）。

        刻意**不看**原始 tui 段——带一堆空值的默认模板（``theme:""`` / ``{}``）
        必须与「完全没有 tui 段」等价。
        """
        if not isinstance(other, Appearance):
            return NotImplemented
        return self._styles == other._styles and self._symbols == other._symbols

    def __repr__(self) -> str:
        return f"Appearance(styles={len(self._styles)}, symbols={len(self._symbols)})"


def resolve_appearance(config: object | None = None) -> Appearance:
    """整份 config.json → Appearance。``config=None`` 时读真实配置文件。

    注意：这里要的是**整份** config（取其中 ``tui`` 段），
    与 :func:`install_theme` 只吃 ``tui`` 段不同。
    """
    try:
        full: object = load_config() if config is None else config
        if not isinstance(full, dict):
            full = {}
        root = cast(dict[str, Any], full)
        tui_raw: object = root.get("tui")
        tui = cast(dict[str, Any], tui_raw) if isinstance(tui_raw, dict) else {}

        # 样式：出厂默认 ← tui.styles（键白名单，非法值忽略）
        styles = dict(DEFAULT_STYLES)
        user_styles_raw: object = tui.get("styles")
        if isinstance(user_styles_raw, dict):
            for key, value in cast(dict[str, Any], user_styles_raw).items():
                if key in DEFAULT_STYLES:
                    ok = valid_style(value)
                    if ok is not None:
                        styles[key] = ok
                # 未知键 / 非法样式 → 忽略（回落链自然生效）

        # 符号：底表（ascii 分岔）← tui.symbols 覆盖
        base = ASCII_SYMBOLS if tui.get("ascii") is True else DEFAULT_SYMBOLS
        symbols = dict(base)
        user_symbols_raw: object = tui.get("symbols")
        if isinstance(user_symbols_raw, dict):
            for key, value in cast(dict[str, Any], user_symbols_raw).items():
                if key in DEFAULT_SYMBOLS and isinstance(value, str) and value.strip():
                    symbols[key] = value

        return Appearance(styles, symbols, tui)
    except Exception:  # noqa: BLE001 — 外观解析永不阻断 TUI 启动
        return Appearance(dict(DEFAULT_STYLES), dict(DEFAULT_SYMBOLS))


_appearance = Appearance(dict(DEFAULT_STYLES), dict(DEFAULT_SYMBOLS))


def install_appearance(config: object | None = None) -> Appearance:
    """解析并发布当前 Appearance（进程级单次装配），返回它。"""
    global _appearance
    _appearance = resolve_appearance(config)
    return _appearance


def current_appearance() -> Appearance:
    """当前已装配的 Appearance（面板读原始 tui 段 / 自定义标记用）。"""
    return _appearance


def tui_style(key: str) -> str:
    """取语义键的 rich 样式串（tui.py 渲染调用点用）。"""
    return _appearance.style(key)


def tui_glyph(key: str) -> str:
    """取语义键的符号字形（tui.py 渲染调用点用）。"""
    return _appearance.glyph(key)


__all__ = [
    "Appearance",
    "current_appearance",
    "install_appearance",
    "resolve_appearance",
    "tui_glyph",
    "tui_style",
]
