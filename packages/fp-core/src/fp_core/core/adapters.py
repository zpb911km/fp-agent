"""厂商适配层：把 fp 的规范请求语义翻译为各 provider 的原生参数。

设计约束：
- 纯函数、无配置依赖（不 import config）——config.py 负责读出 provider/base_url，
  这里只做"语义 → 原生格式"的翻译，便于单测与后续新增维度。
- 厂商知识收口于此：任何"某厂商参数名/行为特殊"的 if 分流只允许出现在本模块。

现有维度：思考开关。
fp 的规范意图键是布尔 "enable_thinking"（DashScope/qwen 原生参数名），
解析时按目标 provider 翻译为各自的原生请求格式，用户在配置里只需写一种语义：
  - deepseek → {"thinking": {"type": "enabled"/"disabled"}}
    （reasoning_effort 若用户显式提供则保留并平铺到请求体顶层）
  - amd-radeon → {"chat_template_kwargs": {"thinking": bool}}
    （AMD Radeon 网关：enable_thinking 静默忽略、顶层 thinking 400，
      唯一有效开关是 chat_template_kwargs.thinking —— 实测 vLLM 模板参数）
  - glm/bigmodel → 关思考不可表达（400 1210），以 reasoning_effort:"low" 近似
  - 其余（qwen/未知/直连）→ 保持 enable_thinking 布尔
用户直接写的 thinking / reasoning_effort / chat_template_kwargs 原生键不受影响，透传。
"""

from typing import Any

_THINKING_NATIVE_KEYS = ("thinking", "reasoning_effort", "chat_template_kwargs")

# canonical 名 → base_url 关键词（按序匹配，先命中先得）
_PROVIDER_URL_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("deepseek", ("deepseek",)),
    ("glm", ("bigmodel", "zhipu")),
    ("radeon", ("amd", "radeon")),
)


def classify_provider(provider: str) -> str:
    """把 provider 名归一化为 canonical 类别：deepseek / glm / radeon / 其它归 openai 兼容。

    依 provider 名的子串判定（如 "deepseek-chat"、"zhipu-glm" 都能命中）；
    不命中任何已知厂商 → "openai"（OpenAI 兼容默认行为）。
    """
    low = (provider or "").lower()
    if "deepseek" in low:
        return "deepseek"
    if "glm" in low or "bigmodel" in low or "zhipu" in low:
        return "glm"
    if "amd" in low or "radeon" in low:
        return "radeon"
    return "openai"


def provider_hint_from_base(base_url: str) -> str:
    """从 base_url 粗推 provider 名（仅用于无表/无 ACTIVE_LLM 的兜底场景）。

    返回值可直接喂给 classify_provider；不命中返回空串。
    """
    low = (base_url or "").lower()
    for name, keys in _PROVIDER_URL_HINTS:
        if any(k in low for k in keys):
            return name
    return ""


def no_thinking_body_for(provider: str) -> dict[str, Any]:
    """按 provider 返回"关闭思考"的原生 extra_body。

    供轻量任务（会话标题、摘要等）显式禁用思考，避免：
      - thinking 模型非流式 + 小 max_tokens 直接报错/截断
      - 无谓的思考 token 开销
    """
    kind = classify_provider(provider)
    if kind == "deepseek":
        return {"thinking": {"type": "disabled"}}
    if kind == "radeon":
        # AMD Radeon：enable_thinking 是死参数 → 用唯一有效的 vLLM 模板开关
        return {"chat_template_kwargs": {"thinking": False}}
    if kind == "glm":
        # GLM 关不掉思考（enable_thinking:false → 400 1210）→ 最低档近似关闭
        return {"reasoning_effort": "low"}
    return {"enable_thinking": False}


def normalize_thinking_params(provider: str, extra_body: dict[str, Any]) -> dict[str, Any]:
    """把规范意图键 enable_thinking 翻译为目标 provider 的原生请求参数。"""
    out = dict(extra_body)
    intent = out.pop("enable_thinking", None)
    if intent is None:
        return out
    kind = classify_provider(provider)
    if kind == "deepseek":
        # 用户已显式写了 thinking:{...} 原生格式 → 以原生为准，丢弃意图键
        if "thinking" not in out:
            out["thinking"] = {"type": "enabled"} if intent else {"type": "disabled"}
        # reasoning_effort 若存在则原样保留（llm_client body.update 平铺 → 顶层字段）
    elif kind == "radeon":
        # AMD Radeon 网关实测：
        #   enable_thinking → 静默忽略（死参数）
        #   thinking:{type} → 400 "thinking is not supported ... Use reasoning_effort"
        #   reasoning_effort / reasoning.effort → 也无效（DeepSeek 系恒不思考）
        #   chat_template_kwargs:{thinking} → 唯一有效（vLLM 模板参数）
        # 用户已显式写了 chat_template_kwargs → 以原生为准，不覆盖。
        if "chat_template_kwargs" not in out:
            out["chat_template_kwargs"] = {"thinking": bool(intent)}
    elif kind == "glm":
        # bigmodel GLM 实测（glm-5.3-flash，错误码 1210）：
        #   enable_thinking:false → 400 "该模型始终思考，不支持关闭思考；
        #     请使用 low、high 或 max"；thinking:{type:disabled} 同样 400。
        #   故「关思考」在 GLM 上不可表达，最接近的近似是最低思考档：
        #   reasoning_effort:"low"（实测 0 思考 token）。
        #   开思考 → thinking:{type:"enabled"}（实测 200）。
        # 用户显式写了 reasoning_effort → 直接控档位，意图键无需再翻译。
        if "reasoning_effort" in out:
            pass
        elif intent:
            if "thinking" not in out:
                out["thinking"] = {"type": "enabled"}
        else:
            out["reasoning_effort"] = "low"
    else:
        out["enable_thinking"] = bool(intent)
    return out
