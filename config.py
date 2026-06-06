"""
Agent v2 配置管理
优先级: config.json > 环境变量 > 代码默认值
"""

import json
import os
from typing import Any, Optional

# 项目根目录
PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG_JSON = os.path.join(PROJECT_ROOT, "config.json")


def _load_json_config() -> dict:
    """加载 config.json"""
    if not os.path.isfile(CONFIG_JSON):
        return {}
    try:
        with open(CONFIG_JSON, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg if isinstance(cfg, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


_json_cfg = _load_json_config()


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
# 路径配置
# ═══════════════════════════════════════════════════════════════

PROMPTS_DIR = os.path.join(PROJECT_ROOT, "prompts")
SKILLS_DIR = os.path.join(PROJECT_ROOT, "skills")
DATA_DIR = os.path.join(PROJECT_ROOT, "data")
SESSIONS_DIR = os.path.join(DATA_DIR, "sessions")
MEMORY_DIR = os.path.join(DATA_DIR, "memory")
TASKS_FILE = os.path.join(DATA_DIR, "tasks.json")


# ═══════════════════════════════════════════════════════════════
# 显示配置
# ═══════════════════════════════════════════════════════════════

ANSI_COLORS = {
    "black": "\033[30m", "red": "\033[31m", "green": "\033[32m",
    "yellow": "\033[33m", "blue": "\033[34m", "magenta": "\033[35m",
    "cyan": "\033[36m", "white": "\033[37m",
    "bright_red": "\033[91m", "bright_green": "\033[92m",
    "bright_yellow": "\033[93m", "bright_cyan": "\033[96m",
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
    import sys
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
    """检查 LLM 配置是否完整"""
    if not LLM_API_KEY:
        print("[Warning] LLM_API_KEY not set")
        print("  → 设置方式：① 修改 config.json ② 设置环境变量 OPENAI_API_KEY")
        return False
    return True


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
            "llm_thought": {"color": "magenta", "dim": True},
            "llm_tool": {"color": "yellow"},
        }
    }


def init_config(path: Optional[str] = None):
    """初始化配置文件（如果不存在）"""
    config_path = path or CONFIG_JSON
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
    
    if "--init" in os.sys.argv:
        init_config()