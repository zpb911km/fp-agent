"""外观可配置项的**唯一真相表**。

本模块回答三个问题，且只回答一次（解析层 / 面板 / 文档三方共用）：
1. 有哪些可配置项？—— ``SECTIONS`` → ``Field``
2. 它们叫什么？—— ``Field.label``（面板列表里给人看的中文名，用户不必记键名）
3. 出厂默认是什么？—— ``DEFAULT_STYLES`` / ``DEFAULT_SYMBOLS`` / ``ASCII_SYMBOLS``

**扩张新可配置项 = 只改本文件**：加一个 Field、给个默认值，面板与解析自动跟上，
不必动 panel.py / resolve.py。

不变量（``tests/test_appearance.py::TestSchemaInvariants`` 锁死）：
- ``ASCII_SYMBOLS`` 与 ``DEFAULT_SYMBOLS`` 键集完全一致（ascii 降级不能漏项）
- ``SECTIONS`` 里每个 field.key 在对应默认表中有定义，且任何键不被两个 section 重复收录
- 样式表键集与 ``DEFAULT_STYLES`` 一致

kind 语义（面板据此选编辑控件与校验器，见 validate.py）：
``color`` 颜色 | ``style`` rich 样式串 | ``glyph`` 符号字形
``bool`` 真值开关 | ``number`` 浮点数
"""

from __future__ import annotations

from dataclasses import dataclass

# ── 出厂默认：语义样式（rich 样式串） ──────────────────────────────
# 前 20 键为 TUI 渲染键；后 4 键为 REPL 侧键（display.py 的 apply_style 调用点），
# REPL 未配置时保持无色（style.py 现行为），故默认空串——它们只进白名单与文档。
# 2026-10-11 清掉 7 个「空位键」（改了没反应，白让面板给出死旋钮）：
#   · debug / llm_iteration —— 全仓库零引用
#   · item / llm_output —— 对应函数刻意不着色（display.py 直接 print）
#   · header / sep / shutdown_panel —— shutdown_panel 的 (文本, 样式名) 表里，
#     样式名只被当 sep 控制标志用、着色被忽略（疑似丢着色，另案处理）
DEFAULT_STYLES: dict[str, str] = {
    # TUI 渲染键
    "logo": "bold",
    "model": "cyan",
    "hint": "cyan",
    "keys": "dim",
    "notice": "cyan",
    "info": "cyan",
    "warning": "yellow",
    "error": "red",
    "tool": "magenta",
    "tool_result": "dim",
    "think": "dim",
    "prompt": "bold cyan",
    "completion": "bold cyan",
    "completion_desc": "dim",
    "status_model": "bold cyan",
    "status_session": "dim",
    "status_note": "yellow",
    "status_busy": "bold",
    "status_idle": "green",
    "spark": "cyan",
    # REPL 侧键（display.py 流式渲染 / style.py ANSI 着色）
    "llm_thought": "",
    "llm_tool": "",
    "startup": "",
    "yellow_bold": "",
}

# ── 出厂默认：符号字形（emoji 底表） ────────────────────────────────
DEFAULT_SYMBOLS: dict[str, str] = {
    "bot": "🤖",
    "hint": "💡",
    "keys": "⌨",
    "ask": "❓",
    "stats": "📊",
    "think": "💭",
    "tool": "⚙",
    "queued": "⏳",
    "prompt": "❯",
    "send": "↑",
    "busy": "🔴",
    "idle": "🟢",
    "reject": "⛔",
    "reload": "🔄",
    "stop": "⏹",
    # REPL 专属位（display.py / main.py 的提示行与仪式感输出）
    "paused": "⏸",
    "tool_result": "📋",
    "session_end": "📂",
    "headless": "🖥",
}

# ascii=true 时的符号底表（键集必须与 DEFAULT_SYMBOLS 一致）
ASCII_SYMBOLS: dict[str, str] = {
    "bot": "*",
    "hint": "-",
    "keys": "[K]",
    "ask": "?",
    "stats": "[S]",
    "think": "...",
    "tool": ">",
    "queued": "..",
    "prompt": ">",
    "send": "^",
    "busy": "[!]",
    "idle": "[ok]",
    "reject": "!",
    "reload": "~",
    "stop": "[x]",
    "paused": "[||]",
    "tool_result": "[R]",
    "session_end": "[E]",
    "headless": "[H]",
}


# ── 可配置项描述（面板与文档共用的那份「有哪些键」） ───────────────


@dataclass(frozen=True)
class Field:
    """一个可配置项：键名 + 中文标签 + 取值类型。"""

    key: str
    label: str
    kind: str


@dataclass(frozen=True)
class Section:
    """面板里的一个分页。

    ``path`` 是该分页所有字段在 config.json 里的点路径前缀（Field 的完整路径
    = ``f"{path}.{field.key}"``）；``kind="theme"`` 的分页特殊处理（内置主题列表，
    不是键值对），其 fields 为空。
    """

    id: str
    label: str
    kind: str
    path: str
    help: str
    fields: tuple[Field, ...] = ()


THEME_SECTION = Section(
    id="theme",
    label="主题",
    kind="theme",
    path="tui.theme",
    help="↑↓ 预览，Enter 选定并记入 config.json，Esc 取消（回滚预览）",
)

PALETTE_FIELDS = (
    Field("primary", "主色", "color"),
    Field("accent", "强调色", "color"),
    Field("background", "背景", "color"),
    Field("surface", "表面", "color"),
    Field("panel", "面板", "color"),
    Field("foreground", "前景/文字", "color"),
    Field("secondary", "次色", "color"),
    Field("success", "成功", "color"),
    Field("warning", "警告", "color"),
    Field("error", "错误", "color"),
    Field("boost", "高亮提升", "color"),
    Field("dark", "暗色模式", "bool"),
    Field("luminosity_spread", "明度跨度", "number"),
    Field("text_alpha", "文字透明度", "number"),
)

STYLE_FIELDS = (
    Field("logo", "标题徽标", "style"),
    Field("model", "模型名", "style"),
    Field("hint", "提示", "style"),
    Field("keys", "快捷键提示", "style"),
    Field("notice", "通知", "style"),
    Field("info", "信息", "style"),
    Field("warning", "警告", "style"),
    Field("error", "错误", "style"),
    Field("tool", "工具名", "style"),
    Field("tool_result", "工具结果", "style"),
    Field("think", "思考", "style"),
    Field("prompt", "输入提示符", "style"),
    Field("completion", "补全项", "style"),
    Field("completion_desc", "补全说明", "style"),
    Field("status_model", "状态条·模型名", "style"),
    Field("status_session", "状态条·会话", "style"),
    Field("status_note", "状态条·附注", "style"),
    Field("status_busy", "状态条·忙碌", "style"),
    Field("status_idle", "状态条·空闲", "style"),
    Field("spark", "火花线", "style"),
    Field("llm_thought", "思考流（REPL）", "style"),
    Field("llm_tool", "工具流（REPL）", "style"),
    Field("startup", "启动（REPL）", "style"),
    Field("yellow_bold", "中断提示（REPL）", "style"),
)

SYMBOL_FIELDS = (
    Field("bot", "智能体", "glyph"),
    Field("prompt", "输入提示符", "glyph"),
    Field("send", "发送", "glyph"),
    Field("busy", "忙碌", "glyph"),
    Field("idle", "空闲", "glyph"),
    Field("hint", "提示", "glyph"),
    Field("keys", "快捷键", "glyph"),
    Field("ask", "提问", "glyph"),
    Field("stats", "统计", "glyph"),
    Field("think", "思考", "glyph"),
    Field("tool", "工具", "glyph"),
    Field("queued", "排队", "glyph"),
    Field("reject", "拒绝", "glyph"),
    Field("reload", "重载", "glyph"),
    Field("stop", "中断", "glyph"),
    Field("paused", "暂停", "glyph"),
    Field("tool_result", "工具结果", "glyph"),
    Field("session_end", "会话结束", "glyph"),
    Field("headless", "无头驻留", "glyph"),
)

GENERAL_FIELDS = (Field("ascii", "ASCII 降级（缺字形字体/纯文本终端）", "bool"),)

SECTIONS: tuple[Section, ...] = (
    THEME_SECTION,
    Section(
        id="palette",
        label="配色",
        kind="fields",
        path="tui.palette",
        help="自定义调色板（注册为 fp-custom）；回车改色值，r 恢复默认",
        fields=PALETTE_FIELDS,
    ),
    Section(
        id="styles",
        label="样式",
        kind="fields",
        path="tui.styles",
        help="语义 → rich 样式串（如 bold cyan / #ff8800）；回车编辑，r 恢复默认",
        fields=STYLE_FIELDS,
    ),
    Section(
        id="symbols",
        label="符号",
        kind="fields",
        path="tui.symbols",
        help="语义 → 符号字形；回车编辑，r 恢复默认（空 = 用底表默认）",
        fields=SYMBOL_FIELDS,
    ),
    Section(
        id="general",
        label="通用",
        kind="fields",
        path="tui",
        help="全局开关",
        fields=GENERAL_FIELDS,
    ),
)


def section_by_id(section_id: str) -> Section | None:
    """按 id 取分页（面板消息分发用；未知 id → None）。"""
    for section in SECTIONS:
        if section.id == section_id:
            return section
    return None


__all__ = [
    "ASCII_SYMBOLS",
    "DEFAULT_STYLES",
    "DEFAULT_SYMBOLS",
    "GENERAL_FIELDS",
    "PALETTE_FIELDS",
    "SECTIONS",
    "STYLE_FIELDS",
    "SYMBOL_FIELDS",
    "THEME_SECTION",
    "Field",
    "Section",
    "section_by_id",
]
