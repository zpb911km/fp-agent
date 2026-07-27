"""
fp-terminal 样式模块 — ANSI 着色与截断

从 fp-core/config.py 移出，terminal 专属的终端渲染工具。
"""

import os
import sys

from fp_core.platform_utils import is_windows

ANSI_COLORS = {
    "black": "\033[30m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
    "white": "\033[37m",
    "bright_red": "\033[91m",
    "bright_green": "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_cyan": "\033[96m",
    "bright_white": "\033[97m",
    "default": "",
}


def color_supported() -> bool:
    """检测终端是否支持颜色"""
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False
    if is_windows():
        from fp_core.platform_utils import ansi_supported

        return ansi_supported()
    return sys.stdout.isatty()


def get_display_style(name: str) -> dict:
    """获取显示样式"""
    raw = _json_styles().get(name, {})
    color_name = raw.get("color", "default")
    return {
        "color": ANSI_COLORS.get(color_name, ""),
        "bold": raw.get("bold", False),
        "dim": raw.get("dim", False),
        "italic": raw.get("italic", False),
    }


def _json_styles() -> dict:
    """从 config.json 读取 display_styles 段"""
    import json

    from fp_core.platform_utils import get_config_dir

    path = os.path.join(get_config_dir(), "config.json")
    if os.path.isfile(path):
        try:
            with open(path, encoding="utf-8") as f:
                return json.load(f).get("display_styles", {})
        except (json.JSONDecodeError, OSError):
            pass
    return {}


def apply_style(text: str, name: str) -> str:
    """应用 ANSI 样式"""
    if not color_supported():
        return text
    style = get_display_style(name)
    prefix = style["color"]
    if style["bold"]:
        prefix += "\033[1m"
    if style["dim"]:
        prefix += "\033[2m"
    if style["italic"]:
        prefix += "\033[3m"
    return f"{prefix}{text}\033[0m" if prefix else text


def get_display_truncation(name: str) -> int:
    """获取指定名称的截断长度。 -1 表示不截断。"""
    import json

    from fp_core.platform_utils import get_config_dir

    path = os.path.join(get_config_dir(), "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            val = json.load(f).get("display_truncation", {}).get(name, -1)
        return int(val)
    except (TypeError, ValueError, OSError):
        return -1


def truncate(text: str, name: str) -> str:
    """按名称对应的截断长度截断文本。
    -1 不截断；截断时末尾追加 … <+N chars>。
    """
    n = get_display_truncation(name)
    if n < 0 or len(text) <= n:
        return text
    return text[:n] + f"… <+{len(text) - n} chars>"
