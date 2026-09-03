"""
Agent v2 配置管理
优先级: ~/.config/fp/config.json > 环境变量 > 内置硬编码默认值
"""

import json
import os
import sys
from typing import Any, cast

from fp_core.logger import get_logger
from fp_core.platform_utils import get_config_dir, get_data_dir

# 用户配置（跨平台：Linux XDG 标准 / Windows %APPDATA%）
USER_CONFIG_PATH = os.path.join(get_config_dir(), "config.json")


def _load_json_config() -> dict[str, Any]:
    """加载用户配置，不存在则返回空 dict（回退到 Python 硬编码默认值）"""
    if os.path.isfile(USER_CONFIG_PATH):
        try:
            with open(USER_CONFIG_PATH, encoding="utf-8") as f:
                cfg = json.load(f)
            if isinstance(cfg, dict):
                return cast(dict[str, Any], cfg)
        except (json.JSONDecodeError, OSError):
            pass
    return {}


_json_cfg: dict[str, Any] = _load_json_config()

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
# LLM 供应商/模型两级结构
# ═══════════════════════════════════════════════════════════════
# 旧版把「供应商」与「模型」焊死为一维 key（LLM_PROVIDERS 每个 value 直接带一个
# model 字段），导致：
#   - 一个供应商多个模型 → 复制 api_key/base_url，违反 DRY
#   - 不同供应商模型同名 → 只能靠人为造合成 key（aliyun-qwen）消歧，匹配脆弱
# 新版为两级命名空间：
#   LLM_PROVIDERS = {
#     "<provider>": {
#       "api_key": ..., "base_url": ...,
#       "timeout"?: ..., "retry_count"?: ...,        # 连接类（provider 级）
#       "temperature"?: ..., "max_tokens"?: ...,     # 生成类（provider 级默认）
#       "extra_body"?: {...},
#       "models": { "<model>": { temperature/max_tokens/extra_body 等差异 } }
#     }
#   }
# 激活 = ACTIVE_LLM 键，值为 "provider/model"（唯一引用，取代顶层三键副本）。
# 兼容：顶层三键 LLM_API_KEY/BASE_URL/MODEL 仍保留，作为「无表时的直连配置」，
#       也作为激活态的兼容镜像（/model 切换时同步更新，供旧读者读取）。

ACTIVE_LLM_KEY = "ACTIVE_LLM"
LLM_PROVIDERS_KEY = "LLM_PROVIDERS"

# 模型级差异参数（迁移旧格式时从 provider 对象上剥出这些键下沉到模型）
_LLM_MODEL_KEYS = ("temperature", "max_tokens", "extra_body")


def normalize_providers(raw: Any) -> dict[str, dict[str, Any]]:
    """把 LLM_PROVIDERS 值规范化为两级结构（纯函数，无副作用）。

    - 新格式（value.models 为 dict）→ 原样（坏 models 条目丢弃）
    - 旧格式（value.model 为 str）→ 包装为 {"models": {model: 差异参数}}，
      api_key/base_url/连接参数留在 provider 级，生成参数下沉为模型级差异
    - 无表 / 非 dict / 无任何有效模型 / 名字含斜杠 → 丢弃

    Returns:
        规范化后的 dict[str, dict]；空 dict 表示未配置或全部无效。
    """
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for pname, pval in raw.items():
        if not isinstance(pval, dict):
            continue
        name = str(pname)
        if "/" in name:  # 斜杠是 ACTIVE_LLM 的分隔符，provider 名不得含斜杠
            continue
        models_raw = pval.get("models")
        if isinstance(models_raw, dict) and models_raw:
            models: dict[str, dict[str, Any]] = {}
            for mname, mval in models_raw.items():
                mkey = str(mname)
                if "/" in mkey:
                    continue
                models[mkey] = dict(mval) if isinstance(mval, dict) else {}
            if not models:
                continue
            p = {k: v for k, v in pval.items() if k != "models"}
        else:
            # ── 旧格式：model → models.{model} ──
            legacy_model = pval.get("model")
            if not isinstance(legacy_model, str) or not legacy_model.strip():
                continue
            mkey = legacy_model.strip()
            if "/" in mkey:
                continue
            legacy_diff = {k: v for k, v in pval.items() if k in _LLM_MODEL_KEYS}
            p = {k: v for k, v in pval.items() if k != "model" and k not in _LLM_MODEL_KEYS}
            models = {mkey: dict(legacy_diff) if legacy_diff else {}}
        out[name] = {**p, "models": models}
    return out


def get_llm_providers() -> dict[str, dict[str, Any]]:
    """返回规范化后的 LLM_PROVIDERS 表（空 dict = 未配置/全部无效）。"""
    return normalize_providers(_json_cfg.get(LLM_PROVIDERS_KEY))


def infer_active_from_top(
    providers: dict[str, dict[str, Any]],
    top_model: str,
    top_base: str = "",
    top_key: str = "",
) -> str | None:
    """顶层三键兼容推导：在表中找 model/base_url/api_key 全部命中的 provider。

    旧格式 / 新格式但缺 ACTIVE_LLM 时，顶层三键 = 上次切换留下的激活镜像，
    据此推导 "provider/model"。找不到 → None（调用方决定兜底行为）。
    """
    if not top_model or not providers:
        return None
    top_base = top_base.rstrip("/")
    for pname, p in providers.items():
        if top_model not in p.get("models", {}):
            continue
        if top_base and top_base != str(p.get("base_url", "") or "").rstrip("/"):
            continue
        if top_key and top_key != p.get("api_key", ""):
            continue
        return f"{pname}/{top_model}"
    return None


def _split_active(active: str) -> tuple[str, str] | None:
    """拆分 'provider/model' 为二元组；格式不合法 → None。"""
    provider, sep, model = active.partition("/")
    if not sep or not provider or not model or "/" in model:
        return None
    return provider, model


def _merge_extra_body(*bodies: Any) -> dict[str, Any]:
    """浅合并 extra_body：靠后的覆盖同名键；非 dict 输入忽略。"""
    merged: dict[str, Any] = {}
    for b in bodies:
        if isinstance(b, dict):
            merged.update(b)
    return merged


def resolve_llm_params(provider: str, model: str) -> dict[str, Any] | None:
    """解析 (provider, model) 的完整 LLM 参数。

    覆盖链（低→高）：内置默认 → 顶层全局键(TEMPERATURE/…) → provider 级
    → model 级（extra_body 为浅合并：provider 打底，model 覆盖）。
    表内无此 provider/model → None。
    """
    p = get_llm_providers().get(provider)
    if p is None:
        return None
    m = p.get("models", {}).get(model)
    if m is None:
        return None

    def pick(*cands: Any) -> Any:
        for c in cands:
            if c is not None:
                return c
        return None

    temperature = pick(m.get("temperature"), p.get("temperature"), _value("TEMPERATURE", 0.8))
    max_tokens = pick(m.get("max_tokens"), p.get("max_tokens"), _value("MAX_TOKENS", 32768))
    timeout = pick(p.get("timeout"), _value("TIMEOUT", 300))
    retry_count = pick(p.get("retry_count"), _value("RETRY_COUNT", 3))
    extra_body = _merge_extra_body(p.get("extra_body"), m.get("extra_body"))
    if not extra_body:
        extra_body = {"enable_thinking": False}

    return {
        "provider": provider,
        "model": model,
        "api_key": p.get("api_key", ""),
        "base_url": str(p.get("base_url", "") or ""),
        "temperature": float(temperature),
        "max_tokens": int(max_tokens),
        "timeout": int(timeout),
        "retry_count": int(retry_count),
        "extra_body": dict(extra_body),
    }


def resolve_active_llm() -> dict[str, Any] | None:
    """解析当前激活 LLM 的完整参数。

    优先 ACTIVE_LLM 键（"provider/model"）；缺失/失效时退化为顶层三键推导
    （兼容旧配置，不写盘）。无 providers 表 → None（调用方走顶层直连逻辑）。
    """
    providers = get_llm_providers()
    if not providers:
        return None
    active = _value(ACTIVE_LLM_KEY, "")
    if active:
        pair = _split_active(str(active))
        if pair:
            resolved = resolve_llm_params(*pair)
            if resolved is not None:
                return resolved
    inferred = infer_active_from_top(
        providers,
        str(_value("LLM_MODEL", "") or ""),
        str(_value("LLM_API_BASE_URL", "") or ""),
        _value("LLM_API_KEY", ""),
    )
    if inferred:
        pair = _split_active(inferred)
        if pair:
            resolved = resolve_llm_params(*pair)
            if resolved is not None:
                return resolved
    return None


def set_active_llm_state(
    provider: str,
    model: str,
    api_key: str,
    base_url: str,
    temperature: float,
    max_tokens: int,
    extra_body: dict[str, Any] | None = None,
    timeout: int | None = None,
    retry_count: int | None = None,
) -> None:
    """把模块内存态同步到新激活项（供 /model 热切换成功后调用）。

    更新：
    - _json_cfg["ACTIVE_LLM"] = "provider/model"（唯一真源）
    - _json_cfg 顶层三键 LLM_API_KEY/BASE_URL/MODEL = 激活镜像（兼容旧读者）
    - LLM_* 模块常量（LLM_MODEL/TEMPERATURE/MAX_TOKENS/…）→ 生效值

    注意：顶层 TEMPERATURE/MAX_TOKENS 是「用户全局默认」，不做镜像写回
    （避免上次激活的生效值污染后续无自定义参数模型的解析兜底）。
    """
    _json_cfg[ACTIVE_LLM_KEY] = f"{provider}/{model}"
    _json_cfg["LLM_API_KEY"] = api_key
    _json_cfg["LLM_API_BASE_URL"] = base_url
    _json_cfg["LLM_MODEL"] = model
    globals()["LLM_ACTIVE_ID"] = f"{provider}/{model}"
    globals()["LLM_API_KEY"] = api_key
    globals()["LLM_API_BASE_URL"] = base_url
    globals()["LLM_MODEL"] = model
    globals()["LLM_TEMPERATURE"] = float(temperature)
    globals()["LLM_MAX_TOKENS"] = int(max_tokens)
    globals()["LLM_EXTRA_BODY"] = dict(extra_body) if extra_body is not None else {}
    if timeout is not None:
        globals()["LLM_TIMEOUT"] = int(timeout)
    if retry_count is not None:
        globals()["LLM_RETRY_COUNT"] = int(retry_count)


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

# ── 两级结构：解析激活 & 回填 LLM_* 常量 ────────────────────────
# 有 LLM_PROVIDERS 表时，顶层三键仅是兼容镜像，真源 = ACTIVE_LLM 引用
# （或从顶层三键推导出的 "provider/model"）。模块加载时解析一次，把激活项的
# 完整参数回填到 LLM_* 常量 —— agent.py 等既读常量者零改动即获正确激活。
_resolved_active = resolve_active_llm()
if _resolved_active is not None:
    LLM_ACTIVE_ID: str = f"{_resolved_active['provider']}/{_resolved_active['model']}"
    LLM_EXTRA_BODY: dict[str, Any] = dict(_resolved_active["extra_body"])
    LLM_API_KEY = _resolved_active["api_key"]
    LLM_API_BASE_URL = _resolved_active["base_url"]
    LLM_MODEL = _resolved_active["model"]
    LLM_TEMPERATURE = float(_resolved_active["temperature"])
    LLM_MAX_TOKENS = int(_resolved_active["max_tokens"])
    LLM_TIMEOUT = int(_resolved_active["timeout"])
    LLM_RETRY_COUNT = int(_resolved_active["retry_count"])
else:
    LLM_ACTIVE_ID = ""
    LLM_EXTRA_BODY = {"enable_thinking": False}


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
MEMORY_DIR = os.path.join(_FP_DATA_DIR, "memory")  # 遗留：资产记忆已迁移至三来源，仅保留向后兼容
MEMORY_DIR_LOCAL = os.path.join(".fp", "memory")
TASKS_FILE = os.path.join(_FP_DATA_DIR, "tasks.json")
# 终端输入历史（运行时状态，不属于资产，独立于三来源目录）
INPUT_HISTORY_FILE = os.path.join(_FP_DATA_DIR, "terminal", "_input_history")
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

    # LLM_PROVIDERS 表存在但激活解析失败 → 顶层三键兜底（提示，不阻断）
    if _json_cfg.get(LLM_PROVIDERS_KEY) and not LLM_ACTIVE_ID:
        log.warning(
            "ACTIVE_LLM 无法解析（指向的 provider/model 不在 LLM_PROVIDERS 表中），"
            "当前按顶层 LLM_API_KEY/BASE_URL/MODEL 直连运行。"
        )

    return ok


def get_default_config() -> dict[str, Any]:
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
