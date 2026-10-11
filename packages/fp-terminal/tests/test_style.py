"""tui.styles 新格式的 REPL 侧解析测试（旧 display_styles 格式已废弃）。

覆盖两条读取路径：
- style.py::get_display_style — rich 样式串 → ANSI（REPL 着色）
- display.py::_think_rich_style — rich 样式串 → rich（流式思考渲染）
"""

import contextlib
import json
import os
import tempfile
import unittest
from collections.abc import Generator
from unittest.mock import patch

from fp_cli.style import get_display_style
from fp_core import platform_utils


@contextlib.contextmanager
def _write_config(data: object) -> Generator[None]:
    """把 data 写进临时配置目录，进入时生效、退出时还原。

    隔离靠**打补丁**（patch ``platform_utils.get_config_dir``），而非
    ``XDG_CONFIG_HOME`` 环境变量——后者只在 Linux 生效，macOS/Windows 的
    ``get_config_dir()`` 不读它，测试会悄悄读到真实用户配置目录（#169）。
    ``style.py`` / ``display.py`` 均在函数内 ``from fp_core.platform_utils import
    get_config_dir``，故补丁在调用点命中。

    传 str 时按原样写入（用于构造坏 JSON）。
    """
    tmp = tempfile.mkdtemp(prefix="fp-style-test-")
    cfg_dir = os.path.join(tmp, "fp")
    os.makedirs(cfg_dir, exist_ok=True)
    path = os.path.join(cfg_dir, "config.json")
    with open(path, "w", encoding="utf-8") as f:
        f.write(data if isinstance(data, str) else json.dumps(data))
    with patch("fp_core.platform_utils.get_config_dir", lambda: cfg_dir):
        yield


class TestGetDisplayStyle(unittest.TestCase):
    """style.py：tui.styles rich 样式串 → ANSI"""

    def test_rich_string_parses_to_ansi(self) -> None:
        with _write_config({"tui": {"styles": {"info": "bold bright_red", "hint": "cyan", "llm_tool": "dim magenta"}}}):
            info = get_display_style("info")
            self.assertEqual(info["color"], "\033[91m")
            self.assertTrue(info["bold"])
            self.assertFalse(info["dim"])
            self.assertEqual(get_display_style("hint")["color"], "\033[36m")
            self.assertTrue(get_display_style("llm_tool")["dim"])

    def test_ansi_unsupported_tokens_ignored(self) -> None:
        # hex/underline 等 ANSI 通道不支持 → 忽略该 token，不崩
        with _write_config({"tui": {"styles": {"info": "#ff0000 underline red"}}}):
            style = get_display_style("info")
            self.assertEqual(style["color"], "\033[31m")  # 支持的 token 仍生效
            self.assertFalse(style["bold"])

    def test_default_color_means_no_color(self) -> None:
        with _write_config({"tui": {"styles": {"llm_thought": "default"}}}):
            style = get_display_style("llm_thought")
            self.assertEqual(style["color"], "")
            self.assertFalse(style["bold"])

    def test_legacy_display_styles_fully_ignored(self) -> None:
        # 旧格式（dict 条目）不读不兼容：着色回落无色
        with _write_config({"display_styles": {"info": {"color": "bright_magenta", "bold": True}}}):
            style = get_display_style("info")
            self.assertEqual(style["color"], "")
            self.assertFalse(style["bold"])

    def test_bad_json_returns_empty(self) -> None:
        with _write_config("{not json"):
            self.assertEqual(get_display_style("info")["color"], "")

    def test_non_dict_sections_returns_empty(self) -> None:
        for bad in ({"tui": "x"}, {"tui": {"styles": 42}}, {"tui": {"styles": {"info": 5}}}):
            with _write_config(bad):
                self.assertEqual(get_display_style("info")["color"], "")

    def test_missing_config_returns_empty(self) -> None:
        tmp = tempfile.mkdtemp(prefix="fp-style-empty-")
        cfg_dir = os.path.join(tmp, "fp")
        # 隔离配置目录：patch 路径来源函数（而非 XDG env，见 _write_config 说明）
        with patch("fp_core.platform_utils.get_config_dir", lambda: cfg_dir):
            self.assertEqual(platform_utils.get_config_dir(), cfg_dir)
            self.assertEqual(get_display_style("info")["color"], "")


class TestThinkRichStyle(unittest.TestCase):
    """display.py：llm_thought 的 rich 样式串读取"""

    def setUp(self) -> None:
        from fp_cli.display import LLMStreamer  # 延迟导入，避免顶层重依赖

        self._fn = LLMStreamer._think_rich_style  # pyright: ignore[reportPrivateUsage] — 测其解析契约

    def test_reads_string_from_tui_styles(self) -> None:
        with _write_config({"tui": {"styles": {"llm_thought": "dim cyan"}}}):
            self.assertEqual(self._fn(), "dim cyan")

    def test_invalid_style_returns_empty(self) -> None:
        with _write_config({"tui": {"styles": {"llm_thought": "not-a-style!!"}}}):
            self.assertEqual(self._fn(), "")

    def test_legacy_dict_returns_empty(self) -> None:
        with _write_config({"display_styles": {"llm_thought": {"color": "cyan", "bold": True}}}):
            self.assertEqual(self._fn(), "")

    def test_missing_returns_empty(self) -> None:
        tmp = tempfile.mkdtemp(prefix="fp-think-empty-")
        with patch.dict(os.environ, {"XDG_CONFIG_HOME": tmp}):
            self.assertEqual(self._fn(), "")


if __name__ == "__main__":
    unittest.main()
