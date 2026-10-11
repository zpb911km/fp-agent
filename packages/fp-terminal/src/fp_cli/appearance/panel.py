"""外观设置面板 — TUI 内现场调外观（主题 / 配色 / 样式 / 符号 / 通用）。

设计分工（这是「config 只记录不交互」的落点）：
- **交互只在这里**：用户不必知道主题名、不必记 ``tui.styles.info`` 这类键名，
      面板把 ``schema.SECTIONS`` 渲染成列表 + 中文标签 + 当前值。
- **config.json 只作记录**：任何改动都经 :func:`configio.persist_tui` 写回；
      下次启动照常生效。

分页与编辑方式（一切从 ``schema.SECTIONS`` 推导，加键不用改本文件）：
    主题   列表（↑↓ 实时预览，Enter 选定并落盘，Esc 回滚预览）
    配色   字段列表（回车改色值 / 切开关，r 恢复默认）
    样式   字段列表（回车编辑 rich 样式串，行内以该样式渲染「Aa 预览」）
    符号   字段列表（回车编辑字形）
    通用   ASCII 降级开关

落盘失败的兜底：persist_tui 返回 False 时进页脚提示（不弹窗、不打断交互）——
外观偏好写不进去不是致命错误，当前会话仍然照改照生效。
"""

from __future__ import annotations

import contextlib
from typing import Any, cast

from rich.text import Text
from textual import on
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Container
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static, TabbedContent, TabPane
from textual.widgets.option_list import Option
from wcwidth import wcswidth

from .configio import UNSET, persist_tui
from .resolve import current_appearance, install_appearance
from .schema import SECTIONS, Field, Section, section_by_id
from .theme import (
    CUSTOM_THEME_NAME,
    base_palette,
    builtin_theme_names,
    install_theme,
)
from .validate import valid_color, validate

# 主题列表里的「默认」行：删掉 tui.theme → 回 Textual 出厂主题
_DEFAULT_OPTION_ID = "__default__"
_DEFAULT_LABEL = "默认（textual-dark）"

# 行内标签对齐宽度（CJK 按双宽计，用 wcwidth 而不是 len）
_LABEL_WIDTH = 30


def _pad(text: str, width: int = _LABEL_WIDTH) -> str:
    """按**显示宽度**补空格（中文/emoji 双宽，len() 对齐会歪）。"""
    w = wcswidth(text)
    if w < 0:  # 含不可打印字符 → 不补
        return text
    return text + " " * max(1, width - w)


class ValuePrompt(ModalScreen[str | None]):
    """单值编辑弹窗：Enter 确认 / Esc 取消 / **空值 = 恢复默认**。

    返回：``None`` = 取消；``""`` = 恢复默认；其余 = 用户输入的原文（调用方校验）。
    """

    BINDINGS = [Binding("escape", "cancel", "取消")]

    CSS = """
    ValuePrompt { align: center middle; }
    #prompt_box {
        width: 64; height: auto;
        border: round $accent; background: $surface; padding: 1 2;
    }
    #prompt_title { height: 1; text-style: bold; }
    #prompt_hint { height: 1; color: $text-muted; }
    """

    def __init__(self, title: str, value: str, hint: str) -> None:
        super().__init__()
        self._title = title
        self._value = value
        self._hint = hint

    def compose(self) -> ComposeResult:
        with Container(id="prompt_box"):
            yield Static(self._title, id="prompt_title")
            yield Input(value=self._value, placeholder=self._hint, id="prompt_input")
            yield Static("Enter 确认 · 留空 = 恢复默认 · Esc 取消", id="prompt_hint")

    def on_mount(self) -> None:
        box = self.query_one("#prompt_input", Input)
        box.focus()
        box.cursor_position = len(box.value)

    @on(Input.Submitted)
    def _submitted(self, event: Input.Submitted) -> None:
        event.stop()
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class AppearancePanel(ModalScreen[None]):
    """外观设置面板（Ctrl+T）。"""

    BINDINGS = [
        Binding("escape", "close_panel", "关闭"),
        Binding("r", "reset_field", "恢复默认"),
    ]

    CSS = """
    AppearancePanel { align: center middle; }
    #panel_box {
        width: 88; height: 30;
        border: round $accent; background: $surface; padding: 0 1;
    }
    #panel_title { height: 1; text-style: bold; color: $accent; }
    #panel_help { height: 1; color: $text-muted; }
    #panel_footer { height: 1; }
    TabbedContent { height: 1fr; }
    OptionList { background: $surface; }
    """

    def __init__(self) -> None:
        super().__init__()
        # 主题预览的回滚基线：Enter 落盘后上移，Esc 关闭时回到这里
        self._baseline_theme: str | None = None
        # 程序化改高亮时压掉随之而来的 OptionHighlighted（否则 _fill 会把
        # 「重置高亮到当前主题」误当作用户预览，进而静默改主题）
        self._skip_highlight: dict[tuple[str, int], int] = {}

    # ── 主题（预览 / 回滚 / 落盘） ───────────────────────────

    def _host(self) -> App[Any]:
        """宿主 App —— 用 ``App[Any]`` 而不是 FPApp：面板刻意不反向依赖 tui.py。

        textual 的 ``Screen.app`` 返回类型带 Unknown（泛型参数），strict 下单点豁免
        （与 tui.py 里 ``cast("FPApp", self.app)`` 的既有处理同理）。
        """
        return cast("App[Any]", self.app)  # pyright: ignore[reportUnknownMemberType]

    def _preview_theme(self, name: str) -> None:
        """改主题但不落盘（走 App 的抑制通道，见 FPApp.apply_theme_silently）。"""
        app = self._host()
        apply_silently = getattr(app, "apply_theme_silently", None)
        if callable(apply_silently):
            apply_silently(name)
        elif name in app.available_themes and app.theme != name:  # 兜底：宿主 App 不是 FPApp
            app.theme = name

    # ── 组装 ────────────────────────────────────────────────

    def compose(self) -> ComposeResult:
        with Container(id="panel_box"):
            yield Static("外观设置", id="panel_title")
            yield Static("", id="panel_help")
            with TabbedContent(id="tabs"):
                for section in SECTIONS:
                    with TabPane(section.label, id=f"tab-{section.id}"):
                        yield OptionList(id=self._list_id(section))
            yield Static("", id="panel_footer")

    def on_mount(self) -> None:
        # 预览走 apply_theme_silently（App 侧按信号计数抑制落盘），面板不需要
        # 自己开关抑制——那套「置 True / unmount 置回 False」被信号的两跳投递
        # 打败过，别再回去（见 FPApp._silent_theme_changes）。
        self._baseline_theme = self._host().theme
        for section in SECTIONS:
            self._fill(section)
        self._refresh_help()
        self._focus_list(self.query_one(TabbedContent).active)

    # ── 行内容 ──────────────────────────────────────────────

    @staticmethod
    def _list_id(section: Section) -> str:
        return f"list-{section.id}"

    def _effective(self, section: Section, field: Field) -> object:
        """该字段当前生效值（面板显示用；未配置 → 回落到默认）。"""
        tui = current_appearance().tui
        if section.id == "palette":
            pal = tui.get("palette")
            if isinstance(pal, dict) and field.key in cast(dict[str, Any], pal):
                return cast(dict[str, Any], pal)[field.key]
            return base_palette().get(field.key)
        if section.id == "styles":
            return current_appearance().style(field.key)
        if section.id == "symbols":
            return current_appearance().glyph(field.key)
        if section.id == "general":
            value = tui.get(field.key)
            return value if isinstance(value, bool) else False
        return ""

    def _value_cell(self, section: Section, field: Field, value: object) -> Text:
        """把生效值渲染成「看得见」的一格（颜色打色块、样式就地渲染预览）。"""
        cell = Text()
        raw = "" if value is None else str(value)
        if field.kind == "color":
            if not raw:
                cell.append("（未设置）", style="dim")
            elif valid_color(raw):
                cell.append("███", style=raw)
                cell.append(f" {raw}", style="dim")
            else:  # 坏值：仍显示（用户要知道 config 里写了什么），但不照着渲染
                cell.append("███", style="red")
                cell.append(f" {raw}（非法，已忽略）", style="red")
        elif field.kind == "style":
            if raw:
                cell.append("Aa 样式预览", style=raw)
                cell.append(f"  {raw}", style="dim")
            else:
                cell.append("（无色）", style="dim")
        elif field.kind == "glyph":
            cell.append(raw)
        elif field.kind == "bool":
            cell.append("开" if value else "关", style="green" if value else "dim")
        else:
            cell.append(raw, style="dim")
        return cell

    def _row(self, section: Section, field: Field) -> Text:
        label = f"{field.label}({field.key})"
        row = Text(_pad(label))
        row.append_text(self._value_cell(section, field, self._effective(section, field)))
        if current_appearance().is_custom(f"{section.path}.{field.key}"):
            row.append("   ✦已自定义", style="dim")
        return row

    def _theme_row(self, name: str, current: str) -> Text:
        from textual.theme import BUILTIN_THEMES

        row = Text(_pad(name, 24))
        theme = BUILTIN_THEMES.get(name)
        if theme is not None:  # 色块让「不知道主题名」的用户也能凭眼睛选
            # ansi-dark/light 的字段是设计令牌（"ansi_blue"）而非颜色，rich 解析不了
            # → 逐个判合法性再上色，免得一行色块把整个列表渲染搞崩
            for candidate in (theme.background, theme.surface, theme.primary, theme.accent):
                if valid_color(str(candidate)):
                    row.append("███", style=str(candidate))
        if name == current:
            row.append("   ● 当前", style="bold")
        return row

    def _fill(self, section: Section) -> None:
        """（重）填一个分页的列表。"""
        options = self.query_one(f"#{self._list_id(section)}", OptionList)
        options.clear_options()
        if section.kind == "theme":
            current = self._host().theme
            default_row = Text(_pad(_DEFAULT_LABEL, 24))
            if current == "textual-dark":
                default_row.append("   ● 当前", style="bold")
            options.add_option(Option(default_row, id=_DEFAULT_OPTION_ID))
            for name in builtin_theme_names():
                options.add_option(Option(self._theme_row(name, current), id=name))
            names = builtin_theme_names()
            target = names.index(current) + 1 if current in names else 0
        else:
            for field in section.fields:
                options.add_option(Option(self._row(section, field), id=field.key))
            target = 0
        if options.option_count:
            self._set_highlight(options, min(target, options.option_count - 1))

    def _set_highlight(self, options: OptionList, index: int) -> None:
        """程序化改高亮，并登记「下一条本列表本下标的高亮消息要吞掉」。"""
        if options.highlighted == index:
            return
        key = (options.id or "", index)
        self._skip_highlight[key] = self._skip_highlight.get(key, 0) + 1
        options.highlighted = index

    # ── 交互 ────────────────────────────────────────────────

    def _section_of(self, option_list: OptionList) -> Section | None:
        return section_by_id(option_list.id.removeprefix("list-") if option_list.id else "")

    def _focus_list(self, tab_id: str) -> None:
        section = section_by_id(tab_id.removeprefix("tab-"))
        if section is None:
            return
        self.query_one(f"#{self._list_id(section)}", OptionList).focus()

    @on(TabbedContent.TabActivated)
    def _tab_activated(self, event: TabbedContent.TabActivated) -> None:
        pane_id = event.pane.id or ""
        self._refresh_help(pane_id)
        self._focus_list(pane_id)

    @on(OptionList.OptionHighlighted)
    def _highlighted(self, event: OptionList.OptionHighlighted) -> None:
        """主题分页：↑↓ 即实时预览（不落盘，Enter 才落）。"""
        event.stop()
        section = self._section_of(event.option_list)
        if section is None or section.kind != "theme":
            return
        key = (event.option_list.id or "", event.option_index)
        pending = self._skip_highlight.get(key, 0)
        if pending:  # _fill 自己压的高亮 → 不是用户预览，别改主题
            self._skip_highlight[key] = pending - 1
            return
        name = event.option.id
        if name == _DEFAULT_OPTION_ID or name is None:
            self._preview_theme("textual-dark")
        else:
            self._preview_theme(name)
        self._refresh_help()

    @on(OptionList.OptionSelected)
    def _selected(self, event: OptionList.OptionSelected) -> None:
        event.stop()
        section = self._section_of(event.option_list)
        key = event.option.id
        if section is None or key is None:
            return
        if section.kind == "theme":
            self._commit_theme(key)
            return
        field = next((f for f in section.fields if f.key == key), None)
        if field is None:
            return
        path = f"{section.path}.{field.key}"
        if field.kind == "bool":
            self._commit(path, not bool(self._effective(section, field)), section)
            return
        value = str(self._effective(section, field))
        title = f"{field.label}（{path}）"
        self._host().push_screen(ValuePrompt(title, value, self._hint_for(field)), self._on_value(section, field, path))

    def _on_value(self, section: Section, field: Field, path: str):  # noqa: ANN201 — 回调闭包
        def _apply(result: str | None) -> None:
            if result is None:
                return  # Esc = 放弃编辑
            if not result.strip():
                self._commit(path, UNSET, section)  # 留空 = 恢复默认
                return
            value = validate(field.kind, result)
            if value is None:
                self._say(f"✗ {field.label}：{result!r} 不是合法的 {field.kind} 值，未改动", "error")
                return
            self._commit(path, value, section)

        return _apply

    @staticmethod
    def _hint_for(field: Field) -> str:
        return {
            "color": "#1e1e2e / red / rgb(20,30,40)",
            "style": "bold cyan / #ff8800 / reverse",
            "glyph": "任意字符，如 ★ 或 [FP]",
            "number": "如 0.8",
        }.get(field.kind, "")

    def _commit_theme(self, name: str) -> None:
        path_value: object = UNSET if name == _DEFAULT_OPTION_ID else name
        theme_name = "textual-dark" if name == _DEFAULT_OPTION_ID else name
        self._preview_theme(theme_name)  # 面板自己落盘 → 走静默通道，别让 watcher 再写一次
        self._baseline_theme = theme_name  # 之后的 Esc 回滚到「已确认的这一个」
        written = persist_tui({"tui.theme": path_value})
        label = _DEFAULT_LABEL if name == _DEFAULT_OPTION_ID else name
        self._say(
            f"✔ 主题 → {label}" + ("（已记入 config.json）" if written else "（无变化）"),
            "green",
        )
        self._fill(section_by_id("theme") or SECTIONS[0])
        self._refresh_help()

    def _commit(self, path: str, value: object, section: Section) -> None:
        """落盘 + 立即生效 + 刷新面板行。

        配色分页有一步额外动作：**改配色 = 切到自定义主题**，故顺手删掉
        ``tui.theme``。否则「theme=nord + palette=...」会按准入顺序让内置主题
        赢，用户改完颜色什么都不会变（两者本就是互斥的两种玩法）。
        """
        updates: dict[str, object] = {path: value}
        note = ""
        if section.id == "palette" and value is not UNSET and self._host().theme != CUSTOM_THEME_NAME:
            updates["tui.theme"] = UNSET
            note = " · 已切到自定义配色（清掉 tui.theme）"
        written = persist_tui(updates)
        self._apply(section)
        self._fill(section)
        verb = "恢复默认" if value is UNSET else "已更新"
        self._say(
            f"✔ {path} {verb}" + ("（已记入 config.json）" if written else "（无变化）") + note,
            "green",
        )

    def _apply(self, section: Section) -> None:
        """把 config 现状重新装配进运行中的 App（改完即时可见）。"""
        install_appearance()
        if (
            section.id == "palette"
            and install_theme(self._host()) is None  # palette 被清空
            and self._host().theme == CUSTOM_THEME_NAME  # → 别再挂着一个空的自定义主题
        ):
            self._preview_theme("textual-dark")
        if section.id == "styles" or section.id == "symbols" or section.id == "general":
            self._refresh_help()

    def action_reset_field(self) -> None:
        """r：把当前高亮项从 config 里删掉（回落出厂默认）。"""
        section = section_by_id(self.query_one(TabbedContent).active.removeprefix("tab-"))
        if section is None:
            return
        options = self.query_one(f"#{self._list_id(section)}", OptionList)
        idx = options.highlighted
        if idx is None:
            return
        key = options.get_option_at_index(idx).id
        if key is None:
            return
        path = "tui.theme" if section.kind == "theme" else f"{section.path}.{key}"
        if not current_appearance().is_custom(path):
            self._say(f"· {path} 本来就是默认值，无需恢复", "dim")
            return
        if section.kind == "theme":
            self._commit_theme(_DEFAULT_OPTION_ID)
            return
        self._commit(path, UNSET, section)

    def action_close_panel(self) -> None:
        """Esc：放弃未确认的主题预览，其余改动均已落盘。"""
        if self._baseline_theme and self._host().theme != self._baseline_theme:
            self._preview_theme(self._baseline_theme)  # 回滚也不落盘（见 apply_theme_silently）
        self.dismiss(None)

    # ── 文案 ────────────────────────────────────────────────

    def _refresh_help(self, tab_id: str | None = None) -> None:
        if tab_id is None:
            tab_id = self.query_one(TabbedContent).active
        section = section_by_id(tab_id.removeprefix("tab-"))
        help_text = section.help if section else ""
        if section is not None and section.kind == "theme":
            help_text = f"当前主题：{self._host().theme} · " + help_text
        self.query_one("#panel_help", Static).update(help_text)

    def _say(self, message: str, style: str) -> None:
        with contextlib.suppress(Exception):  # 面板已卸载时忽略
            self.query_one("#panel_footer", Static).update(Text(message, style=style))


__all__ = ["AppearancePanel", "ValuePrompt"]
