"""fp_cli.appearance — TUI/REPL 外观的**唯一真相与唯一落盘通道**。

外观 = 五类可配置项，全部写在 config.json 的 ``tui`` 段（详见 schema.py）：:

    "tui": {
      "theme": "dracula",        // Textual 内置主题名（21 个）或 "custom"
      "palette": {                // 自定义调色板（注册为 fp-custom）
        "background": "#1e1e2e", "surface": "#181825", "panel": "#313244",
        "accent": "#89b4fa", "primary": "#89b4fa", "foreground": "#cdd6f4",
        "dark": true
      },
      "styles":  { "info": "cyan", "prompt": "bold cyan", ... },  // rich 样式串
      "symbols": { "bot": "🤖", "prompt": "❯", "send": "↑", ... },  // 符号字形
      "ascii": false             // true = 符号表整体换 ASCII 降级
    }

**用户不需要手写这段**：TUI 里 Ctrl+T 打开 :class:`AppearancePanel` 现场改
（列表 + 中文标签 + 实时预览），改完自动写回 config.json；config 只作记录载体。

模块分工（加一项新可配置项通常只需动 schema.py）：:

    schema.py    有哪些键、叫什么、默认值      ← 唯一真相表
    validate.py  什么算合法值（rich 判定）      ← 判定与渲染同源
    configio.py  读写 config.json              ← 唯一落盘通道（persist_tui）
    theme.py     调色板 → Textual 主题          ← app.theme 的唯一准入闸口
    resolve.py   语义键 → 渲染取值（单例）      ← tui_style / tui_glyph
    panel.py     Ctrl+T 设置面板                ← 唯一的交互面

回落纪律（任何坏数据都不得破坏启动，逐级降级到当前默认外观）：
- config.json 缺失 / 坏 JSON / ``tui`` 段不是 dict → 视同无配置
- ``theme`` 不是内置主题名（且非 "custom"）→ 忽略，继续查 palette
- palette 里单个非法颜色 → 丢弃该键，其余照用
- 全部解析失败 → 不设 ``app.theme``，保持 Textual 默认（textual-dark）
- ``tui.styles`` 是 REPL（style.py / display.py）与 TUI 共用的唯一样式表；
  未覆盖的键回落出厂默认；非法 rich 样式 / 未知键 → 忽略
  （旧 ``display_styles`` 格式已废弃，不读不兼容）
- ``ascii=true`` 时符号表整体换 ASCII 底表（REPL 与 TUI 同时生效），``tui.symbols``
  显式覆盖仍优先；``tui.symbols`` 同样是两端共用的唯一符号表，未知键 / 空值 → 忽略
"""

from .configio import UNSET, load_config, load_tui_config, persist_tui
from .panel import AppearancePanel, ValuePrompt
from .resolve import (
    Appearance,
    current_appearance,
    install_appearance,
    resolve_appearance,
    tui_glyph,
    tui_style,
)
from .schema import (
    ASCII_SYMBOLS,
    DEFAULT_STYLES,
    DEFAULT_SYMBOLS,
    SECTIONS,
    Field,
    Section,
    section_by_id,
)
from .theme import (
    BASE_THEME_NAME,
    CUSTOM_THEME_NAME,
    base_palette,
    build_theme,
    builtin_theme_names,
    install_theme,
)
from .validate import valid_color, valid_glyph, valid_number, valid_style, validate

__all__ = [
    "ASCII_SYMBOLS",
    "BASE_THEME_NAME",
    "CUSTOM_THEME_NAME",
    "DEFAULT_STYLES",
    "DEFAULT_SYMBOLS",
    "SECTIONS",
    "UNSET",
    "Appearance",
    "AppearancePanel",
    "Field",
    "Section",
    "ValuePrompt",
    "base_palette",
    "build_theme",
    "builtin_theme_names",
    "current_appearance",
    "install_appearance",
    "install_theme",
    "load_config",
    "load_tui_config",
    "persist_tui",
    "resolve_appearance",
    "section_by_id",
    "tui_glyph",
    "tui_style",
    "validate",
    "valid_color",
    "valid_glyph",
    "valid_number",
    "valid_style",
]
