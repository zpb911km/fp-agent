"""
Agent v2 配置管理
优先级: 环境变量 > ~/.config/fp/config.json > 内置硬编码默认值
"""

import json
import os
import sys
from typing import Any

from fp_core.platform_utils import get_config_dir, get_data_dir, is_windows

# 用户配置（跨平台：Linux XDG 标准 / Windows %APPDATA%）
USER_CONFIG_PATH = os.path.join(get_config_dir(), "config.json")


def _load_json_config() -> dict:
    """加载用户配置，不存在则返回空 dict（回退到 Python 硬编码默认值）"""
    if os.path.isfile(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cfg
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_json_cfg = _load_json_config()

# ═══════════════════════════════════════════════════════════════
# 配置 Schema 验证
# ═══════════════════════════════════════════════════════════════
# 格式: key -> (期望类型, 最小值, 最大值, 是否必填, 描述)
_CONFIG_SCHEMA: dict[str, tuple[type, Any, Any, bool, str]] = {
    "LLM_API_KEY": (str, None, None, True, "API 密钥"),
    "LLM_API_BASE_URL": (str, None, None, True, "API 基础地址"),
    "LLM_MODEL": (str, None, None, True, "模型名称"),
    "TEMPERATURE": (float, 0.0, 2.0, False, "生成温度"),
    "MAX_TOKENS": (int, 1, 131072, False, "最大输出 token 数"),
    "TIMEOUT": (int, 5, 600, False, "请求超时秒数"),
    "RETRY_COUNT": (int, 0, 10, False, "失败重试次数"),
    "MAX_ITERATIONS": (int, 1, 500, False, "最大迭代轮次"),
    "MAX_CONTEXT_TOKENS": (int, 256, 131072, False, "上下文窗口 token 数"),
    "MEMORY_MAX_HISTORY": (int, 0, 10_000, False, "记忆保留历史条数"),
}


def _validate_value(key: str, value: Any, verbose: bool = False) -> list[str]:
    """验证单个配置项，返回错误/警告信息列表。"""
    if key not in _CONFIG_SCHEMA:
        return []

    expected_type, lo, hi, required, desc = _CONFIG_SCHEMA[key]
    issues: list[str] = []

    # 1. 必填性检查
    if required and (value is None or (isinstance(value, str) and not value.strip())):
        issues.append(f"[!] {key} ({desc})：必填项未设置")
        return issues  # 后续检查无意义

    if value is None:
        return issues  # 非必填且为 None，跳过后续检查

    # 2. 类型检查
    if not isinstance(value, expected_type):
        # 允许 int 兼容 float（用户可能在 JSON 中写 TEMPERATURE: 1 而非 1.0）
        if expected_type is float and isinstance(value, int):
            pass  # 隐式转换，不报错
        else:
            actual = type(value).__name__
            issues.append(f"[!] {key} ({desc})：期望 {expected_type.__name__} 类型，实际为 {actual}（值: {value!r}）")
            return issues

    # 3. 数值范围检查
    if lo is not None and hi is not None and isinstance(value, (int, float)) and (value < lo or value > hi):
        issues.append(f"[!] {key} ({desc})：值 {value} 超出有效范围 [{lo}, {hi}]")

    return issues


def validate_config(verbose: bool = False) -> list[str]:
    """验证所有配置项，返回所有问题的汇总列表。

    同时检查 JSON 和模块级常量，确保：
    - 必填项已设置
    - 类型正确
    - 数值在合理范围内

    返回 [(严重级别, key, 消息), ...] 列表，空列表表示全部合法。
    """
    all_issues: list[str] = []

    for key in _CONFIG_SCHEMA:
        # 从 JSON 中获取值
        json_val = _json_cfg.get(key) if key in _json_cfg else None
        issues = _validate_value(key, json_val, verbose=verbose)
        all_issues.extend(issues)

    return all_issues


# 模块加载时自动验证配置
_validation_issues = validate_config()
if _validation_issues:
    for msg in _validation_issues:
        print(f"[Config] {msg}", file=sys.stderr)


def _value(key: str, default: Any = None) -> Any:
    """三优先级取值: JSON > 环境变量 > default"""
    # JSON 中显式指定且不为 null
    if key in _json_cfg and _json_cfg[key] is not None:
        return _json_cfg[key]
    # 环境变量
    env_val = os.getenv(key)
    if env_val is not None:
        # 类型转换
        if key in ("TEMPERATURE",):
            return float(env_val)
        if key in ("MAX_TOKENS", "MAX_ITERATIONS", "TIMEOUT", "RETRY_COUNT", "MAX_CONTEXT_TOKENS"):
            return int(env_val)
        return env_val
    return default


# ═══════════════════════════════════════════════════════════════
# LLM 配置
# ═══════════════════════════════════════════════════════════════

LLM_API_KEY: str = _value("LLM_API_KEY", "")
LLM_API_BASE_URL: str = _value("LLM_API_BASE_URL", "https://api.deepseek.com/v1")
LLM_MODEL: str = _value("LLM_MODEL", "deepseek-v4-flash")
LLM_TEMPERATURE: float = _value("TEMPERATURE", 0.8)
LLM_MAX_TOKENS: int = _value("MAX_TOKENS", 32768)
LLM_TIMEOUT: int = _value("TIMEOUT", 300)
LLM_RETRY_COUNT: int = _value("RETRY_COUNT", 3)


# ═══════════════════════════════════════════════════════════════
# Agent 配置
# ═══════════════════════════════════════════════════════════════

MAX_ITERATIONS: int = _value("MAX_ITERATIONS", 50)
MAX_CONTEXT_TOKENS: int = _value("MAX_CONTEXT_TOKENS", 8000)
MEMORY_MAX_HISTORY: int = _value("MEMORY_MAX_HISTORY", 100)


# ═══════════════════════════════════════════════════════════════
# 路径配置（跨平台：Linux XDG 标准 / Windows %APPDATA%）
# ═══════════════════════════════════════════════════════════════

_FP_DATA_DIR = get_data_dir()

SESSIONS_DIR = os.path.join(_FP_DATA_DIR, "sessions")
MEMORY_DIR = os.path.join(_FP_DATA_DIR, "memory")
TASKS_FILE = os.path.join(_FP_DATA_DIR, "tasks.json")
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")
SKILLS_DIR = os.path.join(os.path.dirname(__file__), "skills")


# ═══════════════════════════════════════════════════════════════
# 显示配置
# ═══════════════════════════════════════════════════════════════

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


def get_display_style(name: str) -> dict:
    """获取显示样式"""
    raw = _json_cfg.get("display_styles", {}).get(name, {})
    color_name = raw.get("color", "default")
    return {
        "color": ANSI_COLORS.get(color_name, ""),
        "bold": raw.get("bold", False),
        "dim": raw.get("dim", False),
        "italic": raw.get("italic", False),
    }


def color_supported() -> bool:
    """检测终端是否支持颜色"""
    if os.environ.get("FORCE_COLOR"):
        return True
    if os.environ.get("NO_COLOR"):
        return False

    if is_windows():
        # Windows 上委托 platform_utils 做更精细的检测
        from fp_core.platform_utils import ansi_supported

        return ansi_supported()

    return sys.stdout.isatty()


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


def truncate(text: str, name: str) -> str:
    """按名称对应的截断长度截断文本。

    -1 不截断；截断时末尾追加 … <+N chars>。
    """
    n = get_display_truncation(name)
    if n < 0 or len(text) <= n:
        return text
    return text[:n] + f"… <+{len(text) - n} chars>"


def get_display_truncation(name: str) -> int:
    """获取指定名称的截断长度。 -1 表示不截断。"""
    val = _json_cfg.get("display_truncation", {}).get(name, -1)
    try:
        return int(val)
    except (TypeError, ValueError):
        return -1


# ═══════════════════════════════════════════════════════════════
# 配置检查
# ═══════════════════════════════════════════════════════════════


def check_llm_config() -> bool:
    """检查 LLM 配置是否完整。

    返回 True 表示配置可用，False 表示存在致命问题（无法联网调用 LLM）。
    会打印所有检测到的问题（含非致命警告）。
    """
    ok = True

    if not LLM_API_KEY:
        print("[Warning] LLM_API_KEY not set")
        print("  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_API_KEY")
        ok = False

    if not LLM_API_BASE_URL:
        print("[Warning] LLM_API_BASE_URL not set")
        print("  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_API_BASE_URL")
        ok = False

    if not LLM_MODEL:
        print("[Warning] LLM_MODEL not set")
        print("  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_MODEL")
        ok = False

    # 打印所有 Schema 验证发现的问题
    global _validation_issues
    if _validation_issues:
        print(f"[Info] 配置文件中存在 {len(_validation_issues)} 个配置问题（详见上方警告）")

    return ok


def get_default_config() -> dict:
    """获取默认配置（用于生成 config.json）"""
    return {
        "LLM_API_KEY": "your-api-key-here",
        "LLM_API_BASE_URL": "https://api.deepseek.com/v1",
        "LLM_MODEL": "deepseek-v4-flash",
        "TEMPERATURE": 0.8,
        "MAX_TOKENS": 32768,
        "TIMEOUT": 300,
        "RETRY_COUNT": 3,
        "MAX_ITERATIONS": 50,
        "MAX_CONTEXT_TOKENS": 8000,
        "MEMORY_MAX_HISTORY": 100,
        "display_styles": {
            "info": {"color": "green"},
            "error": {"color": "bright_red", "bold": True},
            "warning": {"color": "yellow"},
            "yellow_bold": {"color": "yellow", "bold": True},
            "llm_thought": {"color": "magenta", "dim": True},
            "llm_tool": {"color": "yellow"},
        },
    }


def init_config(path: str | None = None):
    """初始化用户配置文件（跨平台：Linux ~/.config/fp/，Windows %APPDATA%/fp/）"""
    config_path = path or USER_CONFIG_PATH
    if os.path.exists(config_path):
        print(f"[Config] {config_path} already exists")
        return

    default_cfg = get_default_config()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_cfg, f, indent=2, ensure_ascii=False)
    print(f"[Config] Created {config_path}")


if __name__ == "__main__":
    # 打印当前配置
    print("=== Agent v2 Configuration ===")
    print(f"LLM_API_KEY: {'***' + LLM_API_KEY[-8:] if LLM_API_KEY else 'NOT SET'}")
    print(f"LLM_API_BASE_URL: {LLM_API_BASE_URL}")
    print(f"LLM_MODEL: {LLM_MODEL}")
    print(f"TEMPERATURE: {LLM_TEMPERATURE}")
    print(f"MAX_TOKENS: {LLM_MAX_TOKENS}")
    print(f"MAX_ITERATIONS: {MAX_ITERATIONS}")
    print()
    check_llm_config()

    if "--init" in sys.argv:
        init_config()
