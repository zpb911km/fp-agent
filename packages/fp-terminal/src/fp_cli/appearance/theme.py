"""调色板 → Textual 主题：内置主题名准入与 ``fp-custom`` 注册。

准入顺序（``install_theme``）：**内置主题名 > palette（自定义）> 保持默认**。

Textual 硬约束：``App.theme`` 的 setter 对未注册主题名抛 ``InvalidThemeError``，
所以必须先查 ``available_themes`` 再赋值——本模块是唯一准入闸口，别处不要直接
写 ``app.theme``（面板预览除外，它走的也是这里）。
"""

from __future__ import annotations

from dataclasses import replace
from typing import TYPE_CHECKING, Any, cast

from textual.theme import BUILTIN_THEMES

from .configio import load_tui_config
from .validate import valid_color

if TYPE_CHECKING:  # pragma: no cover — 仅类型
    from textual.app import App
    from textual.theme import Theme

# palette 注册进 App 时使用的名字（"custom" 是用户侧写法，非主题名）
CUSTOM_THEME_NAME = "fp-custom"
# palette 缺字段时的底板主题（取其全部字段作默认值）
BASE_THEME_NAME = "textual-dark"

# Theme dataclass 允许的 palette 字段，按类型分组校验
_COLOR_KEYS = (
    "primary",
    "secondary",
    "warning",
    "error",
    "success",
    "accent",
    "foreground",
    "background",
    "surface",
    "panel",
    "boost",
)
_BOOL_KEYS = ("dark",)
_FLOAT_KEYS = ("luminosity_spread", "text_alpha")


def builtin_theme_names() -> tuple[str, ...]:
    """全部内置主题名（升序）——面板列表与文档的同一份来源。"""
    return tuple(sorted(BUILTIN_THEMES))


def base_palette() -> dict[str, Any]:
    """底板主题（textual-dark）的全部 palette 字段——面板显示「默认值」用。"""
    theme = BUILTIN_THEMES[BASE_THEME_NAME]
    out: dict[str, Any] = {}
    for key in _COLOR_KEYS:
        out[key] = getattr(theme, key)
    for key in (*_BOOL_KEYS, *_FLOAT_KEYS):
        out[key] = getattr(theme, key)
    return out


def build_theme(app: App[Any], palette: dict[str, Any]) -> Theme | None:
    """palette → Theme（底板取 BASE_THEME_NAME，palette 覆盖其字段）。

    返回 None 表示 palette 一个可用字段都没有（不值得注册）。
    """
    base = app.get_theme(BASE_THEME_NAME)
    if base is None:  # pragma: no cover — textual 必带此主题
        return None

    overrides: dict[str, Any] = {}
    for key in _COLOR_KEYS:
        if key in palette and valid_color(palette[key]):
            overrides[key] = palette[key]
        # 非法颜色 → 丢弃该键，回落底板值
    for key in _BOOL_KEYS:
        if key in palette and isinstance(palette[key], bool):
            overrides[key] = palette[key]
    for key in _FLOAT_KEYS:
        if key in palette and isinstance(palette[key], (int, float)) and not isinstance(palette[key], bool):
            overrides[key] = float(palette[key])
    if not overrides:
        return None

    # replace 保留 base 的 variables/ansi 等未覆盖字段；变量表拷贝防串改
    return replace(
        base,
        name=CUSTOM_THEME_NAME,
        variables=dict(base.variables),
        **overrides,
    )


def install_theme(app: App[Any], config: object | None = None) -> str | None:
    """把 config 的 ``tui`` 段应用到 app，返回生效主题名；未应用 → None。

    准入顺序：内置名 > palette（theme="custom" 或仅给 palette）> 保持默认。
    本函数**不抛异常**——TUI 外观解析失败不能阻断启动。
    """
    try:
        tui = load_tui_config() if config is None else config
        if not isinstance(tui, dict):
            return None
        section = cast(dict[str, Any], tui)
        name = section.get("theme")
        palette = section.get("palette")

        # 1) 显式内置主题名（"custom" 不是内置名，落到第 2 步）
        if isinstance(name, str) and name in app.available_themes:
            app.theme = name
            return name

        # 2) 自定义调色板
        if isinstance(palette, dict) and palette:
            theme = build_theme(app, cast(dict[str, Any], palette))
            if theme is not None:
                app.register_theme(theme)
                app.theme = theme.name
                return theme.name

        # 3) 未知名 / 空 palette → 保持 Textual 默认，不设值
        return None
    except Exception:  # noqa: BLE001 — 外观配置永不阻断 TUI 启动
        return None


__all__ = [
    "BASE_THEME_NAME",
    "CUSTOM_THEME_NAME",
    "base_palette",
    "build_theme",
    "builtin_theme_names",
    "install_theme",
]
