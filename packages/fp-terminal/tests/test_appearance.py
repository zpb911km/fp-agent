"""fp_cli.appearance — 契约测试（schema / validate / configio / theme / resolve）。

分层对应（见 fp_cli/appearance/__init__.py）：
- schema：可配置项的真相表（不变量锁死）
- validate：合法值判定（rich 同源）
- configio：落盘的三条安全线
- theme：调色板 → Textual 主题（未注册名硬约束）
- resolve：语义键 → 渲染取值 + 回落链
"""

from __future__ import annotations

import ast
import json
import os
import re
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from textual.app import App

from fp_cli.appearance import (
    ASCII_SYMBOLS,
    CUSTOM_THEME_NAME,
    DEFAULT_STYLES,
    DEFAULT_SYMBOLS,
    SECTIONS,
    UNSET,
    base_palette,
    build_theme,
    builtin_theme_names,
    install_appearance,
    install_theme,
    load_tui_config,
    persist_tui,
    resolve_appearance,
    section_by_id,
    tui_glyph,
    tui_style,
    valid_color,
    valid_number,
    valid_style,
    validate,
)

_APPEARANCE_SRC = Path(__file__).resolve().parent.parent / "src" / "fp_cli" / "appearance"
_CLI_SRC = Path(__file__).resolve().parent.parent / "src" / "fp_cli"


def _app() -> App[Any]:
    return App()


class TestSchemaInvariants(unittest.TestCase):
    """schema.py 是「有哪些键」的唯一真相——扩张新键时这里必须同时成立。"""

    def test_ascii_table_covers_every_symbol(self) -> None:
        self.assertEqual(set(ASCII_SYMBOLS), set(DEFAULT_SYMBOLS))

    def test_every_field_key_exists_in_its_table(self) -> None:
        # 注意：键名可以跨分页重名（palette.warning 与 styles.warning 是两回事），
        # 唯一性按**完整点路径**判，不按裸键名。
        tables = {"styles": DEFAULT_STYLES, "symbols": DEFAULT_SYMBOLS}
        seen: dict[str, str] = {}
        for section in SECTIONS:
            if section.kind != "fields":
                continue
            for field in section.fields:
                table = tables.get(section.id)
                if table is not None:
                    self.assertIn(field.key, table, f"{section.id}.{field.key} 无默认值")
                path = f"{section.path}.{field.key}"
                self.assertNotIn(path, seen, f"{path} 被 {seen.get(path)} 与 {section.id} 重复收录")
                seen[path] = section.id

    def test_style_and_symbol_sections_are_complete(self) -> None:
        # 反方向：默认表里有、面板里没有 = 用户改不到的键（静默漏项）
        by_id = {s.id: s for s in SECTIONS}
        self.assertEqual({f.key for f in by_id["styles"].fields}, set(DEFAULT_STYLES))
        self.assertEqual({f.key for f in by_id["symbols"].fields}, set(DEFAULT_SYMBOLS))

    def test_paths_are_under_tui(self) -> None:
        for section in SECTIONS:
            self.assertTrue(section.path.startswith("tui"), section.path)

    def test_section_lookup(self) -> None:
        self.assertIsNotNone(section_by_id("styles"))
        self.assertIsNone(section_by_id("nope"))


class TestValidate(unittest.TestCase):
    def test_colors(self) -> None:
        for value in ("#1e1e2e", "red", "bright_blue", "rgb(1,2,3)"):
            self.assertTrue(valid_color(value), value)
        for bad in ("", "not-a-color", None, 3, "ansi_default"):
            self.assertFalse(valid_color(bad), repr(bad))

    def test_styles(self) -> None:
        self.assertEqual(valid_style("bold cyan"), "bold cyan")
        self.assertIsNone(valid_style(""))
        self.assertIsNone(valid_style("   "))
        self.assertIsNone(valid_style("not-a-style!!"))
        self.assertIsNone(valid_style(3))

    def test_numbers(self) -> None:
        self.assertEqual(valid_number("0.5"), 0.5)
        self.assertEqual(valid_number(2), 2.0)
        self.assertIsNone(valid_number("x"))
        self.assertIsNone(valid_number(True))  # bool 不是数字

    def test_validate_dispatch(self) -> None:
        self.assertEqual(validate("number", "1.5"), 1.5)
        self.assertEqual(validate("bool", True), True)
        self.assertIsNone(validate("bool", "true"))
        self.assertEqual(validate("glyph", "★"), "★")
        self.assertIsNone(validate("glyph", "  "))
        self.assertIsNone(validate("unknown-kind", "x"))


class TestLoadTuiConfig(unittest.TestCase):
    def test_missing_file(self) -> None:
        self.assertEqual(load_tui_config("/nonexistent/config.json"), {})

    def test_bad_json(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            f.write("{broken")
        try:
            self.assertEqual(load_tui_config(f.name), {})
        finally:
            os.unlink(f.name)

    def test_tui_not_dict(self) -> None:
        for payload in ('{"tui": "dracula"}', '{"tui": [1]}', "{}"):
            with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
                f.write(payload)
            try:
                self.assertEqual(load_tui_config(f.name), {})
            finally:
                os.unlink(f.name)

    def test_valid_section(self) -> None:
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
            json.dump({"tui": {"theme": "dracula"}}, f)
        try:
            self.assertEqual(load_tui_config(f.name), {"theme": "dracula"})
        finally:
            os.unlink(f.name)


class TestInstallTheme(unittest.TestCase):
    def test_builtin_name(self) -> None:
        app = _app()
        self.assertEqual(install_theme(app, {"theme": "dracula"}), "dracula")
        self.assertEqual(app.theme, "dracula")

    def test_unknown_name_falls_back(self) -> None:
        # 回归：未注册名若直接赋值会抛 InvalidThemeError
        app = _app()
        before = app.theme
        self.assertIsNone(install_theme(app, {"theme": "no-such-theme"}))
        self.assertEqual(app.theme, before)

    def test_custom_palette(self) -> None:
        app = _app()
        cfg = {
            "theme": "custom",
            "palette": {
                "background": "#1e1e2e",
                "surface": "#181825",
                "panel": "#313244",
                "accent": "#89b4fa",
                "primary": "#89b4fa",
                "foreground": "#cdd6f4",
                "dark": True,
            },
        }
        self.assertEqual(install_theme(app, cfg), CUSTOM_THEME_NAME)
        self.assertEqual(app.theme, CUSTOM_THEME_NAME)
        cur = app.current_theme
        assert cur is not None
        self.assertEqual(cur.background, "#1e1e2e")
        self.assertEqual(cur.accent, "#89b4fa")
        self.assertTrue(cur.dark)
        # 未覆盖字段从底板主题继承（如 error）
        base = app.get_theme("textual-dark")
        assert base is not None
        self.assertEqual(cur.error, base.error)

    def test_palette_without_theme_key(self) -> None:
        app = _app()
        self.assertEqual(
            install_theme(app, {"palette": {"background": "#101010", "primary": "#ff0000"}}),
            CUSTOM_THEME_NAME,
        )

    def test_invalid_colors_dropped(self) -> None:
        app = _app()
        self.assertIsNone(install_theme(app, {"palette": {"background": "not-a-color"}}))
        name = install_theme(app, {"palette": {"background": "#101010", "primary": "banana"}})
        self.assertEqual(name, CUSTOM_THEME_NAME)
        cur = app.current_theme
        assert cur is not None
        base = app.get_theme("textual-dark")
        assert base is not None
        self.assertEqual(cur.background, "#101010")
        self.assertEqual(cur.primary, base.primary)

    def test_empty_and_non_dict_config(self) -> None:
        app = _app()
        before = app.theme
        self.assertIsNone(install_theme(app, {}))
        self.assertIsNone(install_theme(app, {"theme": "custom", "palette": {}}))
        self.assertIsNone(install_theme(app, "not-a-dict"))
        self.assertEqual(app.theme, before)

    def test_type_garbage_in_palette(self) -> None:
        app = _app()
        name = install_theme(
            app,
            {"palette": {"background": "#101010", "dark": "yes", "luminosity_spread": "x"}},
        )
        self.assertEqual(name, CUSTOM_THEME_NAME)
        cur = app.current_theme
        assert cur is not None
        base = app.get_theme("textual-dark")
        assert base is not None
        self.assertEqual(cur.dark, base.dark)

    def test_empty_tui_block_is_noop(self) -> None:
        # 回归：空 tui 段必须等价于「无配置」——不得改变默认外观
        tui = {"theme": "", "palette": {}, "styles": {}, "symbols": {}, "ascii": False}
        app = _app()
        before = app.theme
        self.assertIsNone(install_theme(app, tui))
        self.assertEqual(app.theme, before)
        self.assertEqual(resolve_appearance({"tui": tui}), resolve_appearance({}))


class TestBuildTheme(unittest.TestCase):
    def test_empty_palette_returns_none(self) -> None:
        self.assertIsNone(build_theme(_app(), {}))

    def test_name_field_not_overridable(self) -> None:
        # palette 里的 name 是保留键，不得覆盖主题注册名
        app = _app()
        theme = build_theme(app, {"background": "#101010", "name": "hijack"})
        assert theme is not None
        self.assertEqual(theme.name, CUSTOM_THEME_NAME)

    def test_builtin_theme_names_and_base_palette(self) -> None:
        names = builtin_theme_names()
        self.assertIn("dracula", names)
        self.assertEqual(list(names), sorted(names))
        palette = base_palette()
        self.assertIn("primary", palette)
        self.assertTrue(valid_color(palette["primary"]))


class TestResolveAppearance(unittest.TestCase):
    def test_defaults_when_no_config(self) -> None:
        ap = resolve_appearance({})
        self.assertEqual(ap.style("info"), "cyan")
        self.assertEqual(ap.style("prompt"), "bold cyan")
        self.assertEqual(ap.glyph("bot"), "🤖")
        self.assertEqual(ap.glyph("send"), "↑")

    def test_unknown_key_returns_builtin_default(self) -> None:
        ap = resolve_appearance({})
        self.assertEqual(ap.style("no_such_key"), "")
        self.assertEqual(ap.glyph("no_such_key"), "")

    def test_user_styles_override(self) -> None:
        ap = resolve_appearance({"tui": {"styles": {"info": "bold green", "prompt": "italic red"}}})
        self.assertEqual(ap.style("info"), "bold green")
        self.assertEqual(ap.style("prompt"), "italic red")
        self.assertEqual(ap.style("warning"), "yellow")

    def test_invalid_and_unknown_style_dropped(self) -> None:
        ap = resolve_appearance({"tui": {"styles": {"info": "not-a-style!!", "bogus_key": "cyan"}}})
        self.assertEqual(ap.style("info"), "cyan")
        self.assertEqual(ap.style("bogus_key"), "")

    def test_display_styles_fully_deprecated(self) -> None:
        cfg = {"display_styles": {"info": {"color": "bright_magenta", "bold": True}, "error": {"color": "default"}}}
        ap = resolve_appearance(cfg)
        self.assertEqual(ap.style("info"), "cyan")
        self.assertEqual(ap.style("error"), "red")
        ap2 = resolve_appearance({**cfg, "tui": {"styles": {"info": "blue"}}})
        self.assertEqual(ap2.style("info"), "blue")

    def test_repl_keys_in_tui_styles_whitelist(self) -> None:
        cfg = {"tui": {"styles": {"llm_thought": "dim cyan", "startup": "bold green", "llm_tool": "white"}}}
        ap = resolve_appearance(cfg)
        self.assertEqual(ap.style("llm_thought"), "dim cyan")
        self.assertEqual(ap.style("startup"), "bold green")
        self.assertEqual(ap.style("llm_tool"), "white")
        self.assertEqual(resolve_appearance({}).style("llm_thought"), "")
        self.assertEqual(resolve_appearance({}).style("item"), "")  # 已删死键：未知键回落空串

    def test_symbols_override(self) -> None:
        ap = resolve_appearance({"tui": {"symbols": {"bot": "PEB", "busy": ">>"}}})
        self.assertEqual(ap.glyph("bot"), "PEB")
        self.assertEqual(ap.glyph("busy"), ">>")
        self.assertEqual(ap.glyph("idle"), "🟢")
        ap2 = resolve_appearance({"tui": {"symbols": {"bot": "  ", "idle": 3}}})
        self.assertEqual(ap2.glyph("bot"), "🤖")
        self.assertEqual(ap2.glyph("idle"), "🟢")

    def test_ascii_table(self) -> None:
        ap = resolve_appearance({"tui": {"ascii": True}})
        for key in DEFAULT_SYMBOLS:
            glyph = ap.glyph(key)
            self.assertTrue(glyph.isascii() and glyph, f"ascii 表键 {key} 缺失或非 ASCII: {glyph!r}")
        ap2 = resolve_appearance({"tui": {"ascii": True, "symbols": {"bot": "●"}}})
        self.assertEqual(ap2.glyph("bot"), "●")

    def test_non_dict_tui_section(self) -> None:
        ap = resolve_appearance({"tui": "garbage"})
        self.assertEqual(ap.style("info"), "cyan")
        self.assertEqual(ap.glyph("bot"), "🤖")

    def test_garbage_root_never_raises(self) -> None:
        for root in ("x", 3, [1], {"tui": 42, "display_styles": "bad"}):
            ap = resolve_appearance(root)
            self.assertEqual(ap.style("info"), "cyan")

    def test_is_custom_reports_explicit_keys(self) -> None:
        # 面板靠它显示「✦已自定义」并决定 r（恢复默认）是否有效
        ap = resolve_appearance({"tui": {"styles": {"info": "blue"}, "symbols": {"bot": "X"}, "theme": "nord"}})
        self.assertTrue(ap.is_custom("tui.styles.info"))
        self.assertTrue(ap.is_custom("tui.symbols.bot"))
        self.assertTrue(ap.is_custom("tui.theme"))
        self.assertFalse(ap.is_custom("tui.styles.error"))
        self.assertFalse(ap.is_custom("tui.palette.background"))
        self.assertFalse(ap.is_custom("nonsense.path"))  # 非法路径不崩

    def test_is_custom_sees_bad_values(self) -> None:
        # 坏值被解析层忽略，但 config 里确实写着 → 面板要能把它删掉
        ap = resolve_appearance({"tui": {"styles": {"info": "not-a-style!!"}}})
        self.assertTrue(ap.is_custom("tui.styles.info"))
        self.assertEqual(ap.style("info"), "cyan")

    def test_tui_property_exposes_raw_section(self) -> None:
        ap = resolve_appearance({"tui": {"palette": {"background": "#101010"}}})
        self.assertEqual(ap.tui["palette"]["background"], "#101010")


class TestInstallAppearance(unittest.TestCase):
    def test_publishes_to_module_accessors(self) -> None:
        try:
            install_appearance({"tui": {"styles": {"info": "blue"}, "symbols": {"bot": "X"}}})
            self.assertEqual(tui_style("info"), "blue")
            self.assertEqual(tui_glyph("bot"), "X")
        finally:
            install_appearance({})

    def test_restores_defaults(self) -> None:
        install_appearance({"tui": {"ascii": True}})
        try:
            self.assertTrue(tui_glyph("bot").isascii())
        finally:
            install_appearance({})
        self.assertEqual(tui_glyph("bot"), "🤖")

    def test_reading_real_config_when_none(self) -> None:
        # config=None → 读真实配置文件（坏路径也不崩）
        try:
            ap = install_appearance()
            self.assertTrue(ap.style("info"))
        finally:
            install_appearance({})


class TestPersistTui(unittest.TestCase):
    """persist_tui() — 外观设置写回 config.json 的唯一通道与安全边界。

    三条安全线：只改指定点路径（不动用户其他配置）；读不到/解析失败时**一个
    字节都不写**（否则会把 LLM 配置冲成只剩 tui 的残骸）；无实际变化不碰文件。
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.cfg = Path(self._tmp.name) / "config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write(self, payload: object) -> None:
        self.cfg.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def _read(self) -> Any:
        return json.loads(self.cfg.read_text(encoding="utf-8"))

    def test_writes_theme_and_preserves_everything_else(self) -> None:
        self._write({"LLM_MODEL": "m", "tui": {"theme": "", "symbols": {"bot": "🤖"}}})
        self.assertTrue(persist_tui({"tui.theme": "dracula"}, str(self.cfg)))
        data = self._read()
        self.assertEqual(data["tui"]["theme"], "dracula")
        self.assertEqual(data["LLM_MODEL"], "m")
        self.assertEqual(data["tui"]["symbols"]["bot"], "🤖")

    def test_creates_nested_layers(self) -> None:
        self._write({"LLM_MODEL": "m"})
        self.assertTrue(persist_tui({"tui.styles.info": "bold green"}, str(self.cfg)))
        self.assertEqual(self._read()["tui"], {"styles": {"info": "bold green"}})

    def test_multiple_updates_in_one_call(self) -> None:
        self._write({"tui": {}})
        self.assertTrue(
            persist_tui({"tui.symbols.bot": "★", "tui.ascii": True, "tui.palette.primary": "#101010"}, str(self.cfg))
        )
        tui = self._read()["tui"]
        self.assertEqual(tui["symbols"]["bot"], "★")
        self.assertIs(tui["ascii"], True)
        self.assertEqual(tui["palette"]["primary"], "#101010")

    def test_unset_deletes_key_and_prunes_empty_parents(self) -> None:
        self._write({"LLM_MODEL": "m", "tui": {"styles": {"info": "cyan", "error": "red"}}})
        self.assertTrue(persist_tui({"tui.styles.info": UNSET}, str(self.cfg)))
        data = self._read()
        self.assertEqual(data["tui"]["styles"], {"error": "red"})
        self.assertEqual(data["LLM_MODEL"], "m")
        # 删到空 → 整段剪掉（等价于「没配过」）
        self.assertTrue(persist_tui({"tui.styles.error": UNSET}, str(self.cfg)))
        self.assertNotIn("tui", self._read())

    def test_unset_on_missing_key_does_not_touch_file(self) -> None:
        self._write({"tui": {"theme": "nord"}})
        before = self.cfg.read_bytes()
        self.assertFalse(persist_tui({"tui.styles.info": UNSET}, str(self.cfg)))
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_same_value_does_not_touch_file(self) -> None:
        self._write({"tui": {"theme": "nord"}})
        before = self.cfg.read_bytes()
        self.assertFalse(persist_tui({"tui.theme": "nord"}, str(self.cfg)))
        self.assertEqual(self.cfg.read_bytes(), before)

    def test_missing_file_is_not_created(self) -> None:
        self.assertFalse(persist_tui({"tui.theme": "nord"}, str(self.cfg)))
        self.assertFalse(self.cfg.exists())

    def test_broken_json_left_untouched(self) -> None:
        self.cfg.write_text("{ not json", encoding="utf-8")
        self.assertFalse(persist_tui({"tui.theme": "nord"}, str(self.cfg)))
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), "{ not json")

    def test_non_dict_root_rejected(self) -> None:
        self.cfg.write_text("[1, 2]", encoding="utf-8")
        self.assertFalse(persist_tui({"tui.theme": "nord"}, str(self.cfg)))
        self.assertEqual(self.cfg.read_text(encoding="utf-8"), "[1, 2]")

    def test_intermediate_non_dict_is_replaced(self) -> None:
        # tui 是个字符串（用户手写坏了）→ 覆盖成 dict 照写不崩
        self._write({"tui": "oops"})
        self.assertTrue(persist_tui({"tui.theme": "nord"}, str(self.cfg)))
        self.assertEqual(self._read()["tui"], {"theme": "nord"})

    def test_no_tmp_leftover(self) -> None:
        self._write({"tui": {}})
        persist_tui({"tui.theme": "nord"}, str(self.cfg))
        self.assertFalse(Path(f"{self.cfg}.tmp").exists())

    def test_written_file_is_valid_json(self) -> None:
        self._write({"tui": {}})
        persist_tui({"tui.symbols.bot": "★"}, str(self.cfg))
        self.assertEqual(self._read()["tui"]["symbols"]["bot"], "★")  # 中文不被转义成 \uXXXX


_EMOJI_RE = re.compile("[\U0001f300-\U0001faff\u2600-\u27bf\u2b00-\u2bff\ufe0f\u23e9-\u23fa]")


def _code_emoji_literals(py: Path) -> list[tuple[int, str]]:
    """扫出模块里**代码中**出现的 emoji 字面量（注释与 docstring 不算）。

    AST 天然排掉注释；再显式剔除 Module/Class/Function 的 docstring 节点，
    剩下的 Constant 字符串 + f-string 片段就是真正会打印给用户的内容。
    """
    tree = ast.parse(py.read_text(encoding="utf-8"))
    docstrings: set[int] = set()
    owners = (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(node, owners) and body:
            first = body[0]
            if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                docstrings.add(id(first.value))
    hits: list[tuple[int, str]] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Constant)
            and isinstance(node.value, str)
            and id(node) not in docstrings
            and _EMOJI_RE.search(node.value)
        ):
            hits.append((node.lineno, node.value))
    return hits


class TestGlyphTableCoverage(unittest.TestCase):
    """REPL 与 TUI 共用同一张 tui.symbols 表——几条守护。

    扫描范围是**渲染模块**（display/main/cli_io/tui）：那里出现的每个符号都该能
    在设置面板里改。appearance/panel.py 不在列——它是设置界面自身的装饰记号
    （✔ ✦ 之类），不是聊天/REPL 的内容符号，也不该进 tui.symbols 表。
    """

    def test_scanner_detects_emoji(self) -> None:
        # 正例：符号表本体满是 emoji，扫描器必须看得见（防"假绿"）
        self.assertGreaterEqual(len(_code_emoji_literals(_APPEARANCE_SRC / "schema.py")), 15)

    def test_prompt_glyph_is_arrow(self) -> None:
        # 回归：prompt 曾是 ❌（误），应为输入行前缀 ❯
        self.assertEqual(DEFAULT_SYMBOLS["prompt"], "❯")

    def test_repl_modules_have_no_hardcoded_emoji(self) -> None:
        # 任何新加的硬编码 emoji 都会在此被拦下——它该进 DEFAULT_SYMBOLS
        for name in ("display.py", "main.py", "cli_io.py", "tui.py"):
            with self.subTest(module=name):
                self.assertEqual(_code_emoji_literals(_CLI_SRC / name), [])

    def test_glyph_calls_reference_known_keys(self) -> None:
        # tui_glyph('typo') 会静默回落空串 → 界面上凭空少一个符号，故断言键存在
        call = re.compile(r"tui_glyph\(\s*['\"]([a-z_]+)['\"]\s*\)")
        seen: set[str] = set()
        for name in ("display.py", "main.py", "cli_io.py", "tui.py"):
            seen.update(call.findall((_CLI_SRC / name).read_text(encoding="utf-8")))
        self.assertTrue(seen, "没扫到任何 tui_glyph 调用点，正则或代码结构变了")
        self.assertEqual(seen - set(DEFAULT_SYMBOLS), set())

    def test_style_calls_reference_known_keys(self) -> None:
        # 同理：tui_style('typo') 静默回落空串 = 该处静默无色
        call = re.compile(r"tui_style\(\s*['\"]([a-z_]+)['\"]\s*\)")
        seen: set[str] = set()
        for name in ("display.py", "main.py", "cli_io.py", "tui.py"):
            seen.update(call.findall((_CLI_SRC / name).read_text(encoding="utf-8")))
        self.assertTrue(seen, "没扫到任何 tui_style 调用点，正则或代码结构变了")
        self.assertEqual(seen - set(DEFAULT_STYLES), set())


if __name__ == "__main__":
    unittest.main()
