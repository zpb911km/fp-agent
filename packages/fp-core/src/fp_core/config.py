"""
Agent v2 配置管理
优先级: 环境变量 > ~/.config/fp/config.json > 内置硬编码默认值
"""

import json
import os
import sys
from typing import Any

from fp_core.logger import get_logger
from fp_core.platform_utils import get_config_dir, get_data_dir

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


# 模块加载时自动验证配置（结果由 check_llm_config() 报告，此处静默）
_validation_issues = validate_config()


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
# Shell 配置
# ═══════════════════════════════════════════════════════════════

BASH_PATH: str = _value("BASH_PATH", "")
"""用户显式指定的 bash 路径（Windows 专用）。

优先级：config.json > 环境变量 > 空字符串（自动检测）。
示例：BASH_PATH = "G:\\Git\\bin\\bash.exe"
"""


# ═══════════════════════════════════════════════════════════════
# 路径配置（跨平台：Linux XDG 标准 / Windows %APPDATA%）
# ═══════════════════════════════════════════════════════════════

_FP_DATA_DIR = get_data_dir()

SESSIONS_DIR = os.path.join(_FP_DATA_DIR, "sessions")
MEMORY_DIR = os.path.join(_FP_DATA_DIR, "memory")
MEMORY_DIR_LOCAL = os.path.join(".fp", "memory")
TASKS_FILE = os.path.join(_FP_DATA_DIR, "tasks.json")
PROMPTS_DIR = os.path.join(os.path.dirname(__file__), "prompts")

# ── 记忆系统禁用分类（不可作为 category 使用） ─────────────────────
FORBIDDEN_CATEGORIES = {"core", "misc", "other", "uncategorized"}

# ═══════════════════════════════════════════════════════════════
# 三来源目录（ext 资产分发系统）
# ═══════════════════════════════════════════════════════════════
# 来源优先级（低→高）：fetched → public → private。
# user_dirs() 返回顺序即加载顺序：后加载覆盖先加载（private 胜出），
# 加载器遇同名冲突时需打警告。
EXT_SOURCES = ("fetched", "public", "private")
EXT_LAYOUTS = {
    "tools": ("tools", "extensions"),
    "commands": ("commands",),
    "plugins": ("plugins",),
    "memory": ("memory",),
}


def user_dirs(kind: str) -> list[str]:
    """返回某资产类型的三来源目录（优先级低→高，加载顺序 fetched→public→private）。

    Args:
        kind: "tools" | "commands" | "plugins" | "memory"

    Returns:
        [fetched_dir, public_dir, private_dir]（均基于 {DATA} 根）
    """
    if kind not in EXT_LAYOUTS:
        raise ValueError(f"未知资产类型: {kind}（可选 {list(EXT_LAYOUTS)}）")
    return [os.path.join(_FP_DATA_DIR, s, *EXT_LAYOUTS[kind]) for s in EXT_SOURCES]


# ═══════════════════════════════════════════════════════════════
# 配置检查
# ═══════════════════════════════════════════════════════════════


def check_llm_config() -> bool:
    """检查 LLM 配置是否完整。

    返回 True 表示配置可用，False 表示存在致命问题（无法联网调用 LLM）。
    通过 Logger 输出警告信息（调用方需先注入 Logger 实现）。
    """
    ok = True
    log = get_logger()

    if not LLM_API_KEY:
        log.warning("LLM_API_KEY not set\n  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_API_KEY")
        ok = False

    if not LLM_API_BASE_URL:
        log.warning("LLM_API_BASE_URL not set\n  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_API_BASE_URL")
        ok = False

    if not LLM_MODEL:
        log.warning("LLM_MODEL not set\n  → 设置方式：① 修改 config.json ② 设置环境变量 LLM_MODEL")
        ok = False

    # 打印所有 Schema 验证发现的问题
    global _validation_issues
    if _validation_issues:
        log.warning(f"配置文件中存在 {len(_validation_issues)} 个配置问题（通过 check_llm_config() 可知详情）")

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
        "BASH_PATH": "",
    }


def init_config(path: str | None = None):
    """初始化用户配置文件（跨平台：Linux ~/.config/fp/，Windows %APPDATA%/fp/）"""
    log = get_logger()
    config_path = path or USER_CONFIG_PATH
    if os.path.exists(config_path):
        log.info(f"[Config] {config_path} already exists")
        return

    # 确保父目录存在（首次运行配置目录可能不存在）
    parent = os.path.dirname(config_path)
    os.makedirs(parent, exist_ok=True)

    default_cfg = get_default_config()
    with open(config_path, "w", encoding="utf-8") as f:
        json.dump(default_cfg, f, indent=2, ensure_ascii=False)
    log.info(f"[Config] Created {config_path}")


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
