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
