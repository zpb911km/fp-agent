"""取值校验 — 「什么算合法值」的唯一真相。

判定与渲染同源：颜色/样式交给 rich 自己解析（``Color.parse`` / ``Style.parse``）。
用 rich 而不是自写正则，因为 rich 认得 ``#hex`` / 16 色名 / 256 色名 /
``rgb(1,2,3)`` 等多种写法——**rich 认得 = 渲染得出来**，用户手写配置时少一层惊吓。

坏值一律返回 None / False，由调用方决定是丢弃还是回落默认；本模块只做判定，
不抛异常（外观配置永不阻断启动）。
"""

from __future__ import annotations

# 面板编辑器 kind → 校验函数名（schema.Field.kind 的取值域）
_KINDS = ("color", "style", "glyph", "bool", "number")


def valid_color(value: object) -> bool:
    """rich 能解析即合法（#hex / 16 色名 / 256 色名均接受）。"""
    if not isinstance(value, str) or not value:
        return False
    return _parse("color", value)


def valid_style(value: object) -> str | None:
    """rich 能解析 → 返回原样式串，否则 None。空串不算合法（空 = 恢复默认）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return value if _parse("style", value) else None


def valid_glyph(value: object) -> str | None:
    """非空字符串即合法（字形可以是一个字符，也可以是 ``[FP]`` 这类多字符）。"""
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def valid_number(value: object) -> float | None:
    """int/float（bool 除外）或可解析的数字串 → float，否则 None。"""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def validate(kind: str, raw: object) -> object | None:
    """按 Field.kind 校验并归一化；非法 → None（未知 kind 也判非法）。

    归一化只做一件实事：``number`` 串转 float（面板 Input 给的是字符串）。
    """
    if kind == "color":
        return raw if valid_color(raw) else None
    if kind == "style":
        return valid_style(raw)
    if kind == "glyph":
        return valid_glyph(raw)
    if kind == "number":
        return valid_number(raw)
    if kind == "bool":
        return raw if isinstance(raw, bool) else None
    return None


def _parse(kind: str, value: str) -> bool:
    try:
        if kind == "color":
            from rich.color import Color

            Color.parse(value)
        else:
            from rich.style import Style

            Style.parse(value)
    except Exception:  # noqa: BLE001 — rich 解析失败 = 非法值
        return False
    return True


__all__ = [
    "validate",
    "valid_color",
    "valid_glyph",
    "valid_number",
    "valid_style",
]
