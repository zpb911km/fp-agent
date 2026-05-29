import json
import os
from typing import Any

# ============================================================
# 路径定义（不依赖外部配置，始终从代码位置推导）
# ============================================================
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")
CONFIG_JSON = os.path.join(PROJECT_ROOT, "config.json")

# ============================================================
# 加载 config.json（可选），优先级: JSON > 环境变量 > 代码默认值
# ============================================================
def _load_json_config():
    """加载 config.json，返回 dict；文件不存在或格式错误则返回空 dict。"""
    if not os.path.isfile(CONFIG_JSON):
        return {}
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        if not isinstance(cfg, dict):
            return {}
        return cfg
    except (json.JSONDecodeError, OSError):
        return {}

_json_cfg = _load_json_config()


def _value(key: str, env_default: Any = None) -> Any:
    """三优先级取值：JSON > 环境变量 > env_default（代码默认值）"""
    # 1. JSON 中显式指定且不为 null
    if key in _json_cfg and _json_cfg[key] is not None:
        return _json_cfg[key]
    # 2. 环境变量
    env_val = os.getenv(key)
    if env_val is not None:
        # 数值型字段做类型转换
        if key in ("TEMPERATURE",):
            return float(env_val)
        if key in ("MAX_TOKENS", "MAX_ITERATIONS", "SIMILAR_RESPONSE_THRESHOLD", "MAX_CONTEXT_TOKENS"):
            return int(env_val)
        return env_val
    # 3. 代码默认值
    return env_default


# ============================================================
# 核心配置项
# ============================================================
OPENAI_API_KEY: str = _value("OPENAI_API_KEY")
OPENAI_API_BASE_URL: str = _value("OPENAI_API_BASE_URL")
MODEL_NAME: str = _value("MODEL_NAME", "deepseek-v4-flash")

if not OPENAI_API_KEY or not OPENAI_API_BASE_URL:
    raise ValueError(
        "OPENAI_API_KEY and OPENAI_API_BASE_URL must be set\n"
        "  → 设置方式：① 修改 config.json  ② 设置环境变量"
    )

# 死循环检测
MAX_ITERATIONS: int = _value("MAX_ITERATIONS", 50)
SIMILAR_RESPONSE_THRESHOLD: int = _value("SIMILAR_RESPONSE_THRESHOLD", 3)
MAX_CONTEXT_TOKENS: int = _value("MAX_CONTEXT_TOKENS", 8000)

# LLM 参数
TEMPERATURE: float = _value("TEMPERATURE", 0.8)
MAX_TOKENS: int = _value("MAX_TOKENS", 32768)


# ═══════════════════════════════════════════════════
# 显示样式 — 从 config.json 读取，按名称注册
# ═══════════════════════════════════════════════════

_ANSI_COLORS = {
    "black":         "\033[30m",
    "red":           "\033[31m",
    "green":         "\033[32m",
    "yellow":        "\033[33m",
    "blue":          "\033[34m",
    "magenta":       "\033[35m",
    "cyan":          "\033[36m",
    "white":         "\033[37m",
    "bright_black":  "\033[90m",
    "bright_red":    "\033[91m",
    "bright_green":  "\033[92m",
    "bright_yellow": "\033[93m",
    "bright_blue":   "\033[94m",
    "bright_magenta":"\033[95m",
    "bright_cyan":   "\033[96m",
    "bright_white":  "\033[97m",
    "default":       "",
}

_BOLD   = "\033[1m"
_DIM    = "\033[2m"
_ITALIC = "\033[3m"
_RESET  = "\033[0m"


def get_display_style(name: str) -> dict:
    """获取指定名称的显示样式。

    返回 {"color": ANSI码, "bold": bool, "dim": bool, "italic": bool}。
    未注册的名称返回无颜色默认样式。
    """
    raw = _json_cfg.get("display_styles", {}).get(name, {})
    color_name = raw.get("color", "default")
    return {
        "color": _ANSI_COLORS.get(color_name, ""),
        "bold": raw.get("bold", False),
        "dim": raw.get("dim", False),
        "italic": raw.get("italic", False),
    }


def get_display_truncation(name: str) -> int:
    """获取指定名称的截断长度。 -1 表示不截断。"""
    val = _json_cfg.get("display_truncation", {}).get(name, -1)
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


def apply_style(text: str, name: str) -> str:
    """按名称对文本应用 ANSI 颜色和样式。

    终端不支持颜色（NO_COLOR / 非 TTY）时返回纯文本。
    """
    if os.environ.get("NO_COLOR") or not hasattr(os, 'getenv') or not os.isatty(0):
        # 粗判：仅 stdout 是 TTY 时才着色
        pass
    if os.environ.get("NO_COLOR"):
        return text
    import sys
    if not sys.stdout.isatty():
        return text

    style = get_display_style(name)
    prefix = style["color"]
    if style["bold"]:
        prefix += _BOLD
    if style["dim"]:
        prefix += _DIM
    if style["italic"]:
        prefix += _ITALIC

    if not prefix:
        return text
    return f"{prefix}{text}{_RESET}"


def truncate(text: str, name: str) -> str:
    """按名称对应的截断长度截断文本。

    -1 不截断；截断时末尾追加 … <+N chars>。
    """
    n = get_display_truncation(name)
    if n < 0 or len(text) <= n:
        return text
    return text[:n] + f"… <+{len(text) - n} chars>"


# ── 可用颜色名列表（供参考） ──────────────────
COLOR_NAMES = list(_ANSI_COLORS.keys())
