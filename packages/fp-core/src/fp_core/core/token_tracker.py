"""
TokenTracker — token 消耗跟踪器

职责：
- 累加每次 LLM 调用的 token 用量
- 按模型分别统计
- 序列化/反序列化（用于会话持久化和恢复）
- 格式化展示

支持的 usage 结构（兼容各厂商 API 差异）：
  {
    "prompt_tokens": int,
    "completion_tokens": int,
    "total_tokens": int,
    "prompt_tokens_details": {"cached_tokens": int},    ← DeepSeek
    "completion_tokens_details": {"reasoning_tokens": int},  ← DeepSeek
    "prompt_cache_hit_tokens": int,                     ← DeepSeek
    "prompt_cache_miss_tokens": int,                     ← DeepSeek
  }

使用方：
  Agent 在 __init__ 中创建，每次 LLM 调用后 accumulate()，
  退出时 to_dict() 写入 session meta，/token 命令读取展示。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast


@dataclass
class TokenUsage:
    """单次/累计的 token 用量（含缓存细分）"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    call_count: int = 0

    # ── 缓存相关（从 usage.prompt_tokens_details.cached_tokens 提取） ──
    cache_hit_tokens: int = 0  # prompt 中命中缓存的部分
    cache_miss_tokens: int = 0  # prompt 中未命中缓存的部分

    # ── 推理相关（从 usage.completion_tokens_details.reasoning_tokens 提取） ──
    reasoning_tokens: int = 0  # 思考过程 token 数

    def accumulate(self, other: TokenUsage) -> None:
        """合并另一份用量"""
        self.prompt_tokens += other.prompt_tokens
        self.completion_tokens += other.completion_tokens
        self.total_tokens += other.total_tokens
        self.call_count += other.call_count
        self.cache_hit_tokens += other.cache_hit_tokens
        self.cache_miss_tokens += other.cache_miss_tokens
        self.reasoning_tokens += other.reasoning_tokens

    @property
    def cache_hit_rate(self) -> float | None:
        """缓存命中率 (0~1)，若无 cache 数据返回 None"""
        total_cache = self.cache_hit_tokens + self.cache_miss_tokens
        if total_cache == 0:
            return None
        return self.cache_hit_tokens / total_cache

    @property
    def cache_hit_rate_str(self) -> str:
        rate = self.cache_hit_rate
        if rate is None:
            return "-"
        return f"{rate:.0%}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "call_count": self.call_count,
        }
        # 仅在存在有效数据时写入缓存字段，避免冗余
        if self.cache_hit_tokens > 0 or self.cache_miss_tokens > 0:
            d["cache_hit_tokens"] = self.cache_hit_tokens
            d["cache_miss_tokens"] = self.cache_miss_tokens
        if self.reasoning_tokens > 0:
            d["reasoning_tokens"] = self.reasoning_tokens
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TokenUsage:
        return cls(
            prompt_tokens=data.get("prompt_tokens", 0),
            completion_tokens=data.get("completion_tokens", 0),
            total_tokens=data.get("total_tokens", 0),
            call_count=data.get("call_count", 0),
            cache_hit_tokens=data.get("cache_hit_tokens", 0),
            cache_miss_tokens=data.get("cache_miss_tokens", 0),
            reasoning_tokens=data.get("reasoning_tokens", 0),
        )

    # ── 展示 ────────────────────────────────────────

    def __str__(self) -> str:
        if self.call_count == 0:
            return "N/A"
        return (
            f"↑{self.prompt_tokens:,} ↓{self.completion_tokens:,} "
            f"(cache: {self.cache_hit_rate_str} hit) ×{self.call_count}次"
        )

    @property
    def short_str(self) -> str:
        """紧凑格式，用于面板一行显示"""
        if self.call_count == 0:
            return "N/A"
        base = f"{self.total_tokens:,} (↑{self.prompt_tokens:,} ↓{self.completion_tokens:,}"
        if self.cache_hit_tokens or self.cache_miss_tokens:
            base += f" cache:{self.cache_hit_rate_str}"
        base += f" ×{self.call_count})"
        return base


class TokenTracker:
    """线程安全的 token 累加器（GIL 保护，无需额外锁）"""

    def __init__(self) -> None:
        self._total = TokenUsage()
        self._per_model: dict[str, TokenUsage] = {}

    # ── 累加 ─────────────────────────────────────────

    def accumulate(self, usage: dict[str, Any] | None, model: str = "") -> None:
        """累加一次 LLM 调用的 token 消耗

        兼容 DeepSeek / OpenAI 等各厂商的 usage 结构差异：
          - 标准字段：prompt_tokens, completion_tokens, total_tokens
          - 缓存字段：prompt_tokens_details.cached_tokens
                     prompt_cache_hit_tokens / prompt_cache_miss_tokens
          - 推理字段：completion_tokens_details.reasoning_tokens

        Args:
            usage: API 返回的 usage dict（可能为 None）
            model: 模型名称（可选，用于按模型细分统计）
        """
        if usage is None:
            return

        prompt: int = usage.get("prompt_tokens", 0) or 0
        completion: int = usage.get("completion_tokens", 0) or 0
        total: int = usage.get("total_tokens", 0) or 0

        if total == 0:
            return  # 无效数据，跳过

        # ── 提取缓存相关字段 ──
        cache_hit: int = 0
        cache_miss: int = 0

        # 方式1: prompt_tokens_details.cached_tokens（DeepSeek 嵌套结构）
        prompt_details = cast(dict[str, Any] | None, usage.get("prompt_tokens_details"))
        if isinstance(prompt_details, dict):
            cache_hit = prompt_details.get("cached_tokens", 0) or 0

        # 方式2: 平铺的 prompt_cache_hit_tokens（也来自 DeepSeek，不同版本）
        if not cache_hit:
            cache_hit = usage.get("prompt_cache_hit_tokens", 0) or 0
        cache_miss = usage.get("prompt_cache_miss_tokens", 0) or 0

        # 如果 cache_hit 从 nested 取了，但 miss 缺失，用 prompt - hit 推算
        if cache_hit > 0 and cache_miss == 0:
            cache_miss = max(0, prompt - cache_hit)

        # ── 提取推理相关字段 ──
        reasoning: int = 0
        completion_details = cast(dict[str, Any] | None, usage.get("completion_tokens_details"))
        if isinstance(completion_details, dict):
            reasoning = completion_details.get("reasoning_tokens", 0) or 0
        if not reasoning:
            reasoning = usage.get("reasoning_tokens", 0) or 0

        u = TokenUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            call_count=1,
            cache_hit_tokens=cache_hit,
            cache_miss_tokens=cache_miss,
            reasoning_tokens=reasoning,
        )

        # 累计到总量
        self._total.accumulate(u)

        # 累计到模型分组
        if model:
            if model not in self._per_model:
                self._per_model[model] = TokenUsage()
            self._per_model[model].accumulate(u)

    # ── 查询 ─────────────────────────────────────────

    @property
    def total(self) -> TokenUsage:
        return self._total

    def for_model(self, model: str) -> TokenUsage:
        """返回指定模型的累计用量（若无则返回空 TokenUsage）"""
        return self._per_model.get(model, TokenUsage())

    @property
    def models(self) -> list[str]:
        """返回有记录的模型名称列表"""
        return list(self._per_model.keys())

    # ── 序列化 ───────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        """序列化为可持久化的 dict（含按模型细分）"""
        result: dict[str, Any] = {
            "total": self._total.to_dict(),
        }
        if self._per_model:
            result["per_model"] = {model: usage.to_dict() for model, usage in self._per_model.items()}
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any] | None) -> TokenTracker:
        """从持久化数据恢复 TokenTracker"""
        tracker = cls()
        if not data:
            return tracker

        total_data = data.get("total")
        if total_data:
            tracker._total = TokenUsage.from_dict(total_data)

        per_model = data.get("per_model", {})
        if per_model:
            tracker._per_model = {model: TokenUsage.from_dict(usage_data) for model, usage_data in per_model.items()}

        return tracker

    # ── 展示（markdown 格式，适配 UI markdown 渲染） ──

    def format_detailed(self, current_model: str = "") -> str:
        """格式化详细统计，输出纯 markdown（用于 /token 命令）"""
        lines: list[str] = []
        lines.append("📊 **Token 消耗统计**")
        lines.append("")

        def _append_usage(name: str, u: TokenUsage) -> None:
            lines.append(f"**{name}**")
            lines.append("")
            lines.append("| 指标 | 数值 |")
            lines.append("|------|-----:|")
            lines.append(f"| 调用次数 | {u.call_count} |")
            lines.append(f"| 输入 (prompt) | {u.prompt_tokens:,} |")
            lines.append(f"| 输出 (completion) | {u.completion_tokens:,} |")
            lines.append(f"| **总计** | **{u.total_tokens:,}** |")
            if u.reasoning_tokens > 0:
                lines.append(f"| 其中思考 (reasoning) | {u.reasoning_tokens:,} |")
            if u.cache_hit_tokens > 0 or u.cache_miss_tokens > 0:
                lines.append(f"| 缓存命中率 | {u.cache_hit_rate_str} |")
                lines.append(f"| 缓存命中 (hit) | {u.cache_hit_tokens:,} |")
                lines.append(f"| 缓存未命中 (miss) | {u.cache_miss_tokens:,} |")
            lines.append("")

        # 当前模型
        if current_model and current_model in self._per_model:
            _append_usage(f"当前模型: `{current_model}`", self._per_model[current_model])

        # 其他模型
        other_models = [m for m in self._per_model if m != current_model]
        if other_models:
            for model in other_models:
                _append_usage(f"模型: `{model}`", self._per_model[model])

        # 总计
        _append_usage("📦 总计", self._total)
        return "\n".join(lines).strip()

    def __str__(self) -> str:
        return self.format_detailed()
