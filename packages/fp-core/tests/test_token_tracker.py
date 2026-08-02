"""测试 TokenTracker / TokenUsage — token 统计与序列化

覆盖重点：
- TokenUsage 聚合、缓存命中率、序列化、展示
- TokenTracker 累加逻辑：标准 usage / DeepSeek 嵌套缓存 / 平铺缓存 / 推理 token
- 无效数据跳过（None / total=0）
- 按模型分组统计
"""

from fp_core.core.token_tracker import TokenTracker, TokenUsage


class TestTokenUsage:
    def test_default_zero(self):
        """默认值全为 0"""
        u = TokenUsage()
        assert u.prompt_tokens == 0
        assert u.completion_tokens == 0
        assert u.total_tokens == 0
        assert u.call_count == 0

    def test_accumulate_sums(self):
        """accumulate() 逐字段求和"""
        a = TokenUsage(
            prompt_tokens=10,
            completion_tokens=5,
            total_tokens=15,
            call_count=1,
            cache_hit_tokens=3,
            cache_miss_tokens=7,
            reasoning_tokens=2,
        )
        b = TokenUsage(
            prompt_tokens=20,
            completion_tokens=10,
            total_tokens=30,
            call_count=1,
            cache_hit_tokens=10,
            cache_miss_tokens=10,
            reasoning_tokens=4,
        )
        a.accumulate(b)
        assert a.prompt_tokens == 30
        assert a.completion_tokens == 15
        assert a.total_tokens == 45
        assert a.call_count == 2
        assert a.cache_hit_tokens == 13
        assert a.cache_miss_tokens == 17
        assert a.reasoning_tokens == 6

    def test_cache_hit_rate_none_when_no_cache(self):
        """无缓存数据时命中率为 None"""
        u = TokenUsage()
        assert u.cache_hit_rate is None
        assert u.cache_hit_rate_str == "-"

    def test_cache_hit_rate_calculates(self):
        """有缓存数据时计算 0~1 命中率"""
        u = TokenUsage(cache_hit_tokens=75, cache_miss_tokens=25)
        assert u.cache_hit_rate == 0.75
        assert u.cache_hit_rate_str == "75%"

    def test_cache_hit_rate_full_hit(self):
        u = TokenUsage(cache_hit_tokens=100, cache_miss_tokens=0)
        assert u.cache_hit_rate == 1.0
        assert u.cache_hit_rate_str == "100%"

    def test_to_dict_omits_zero_optional_fields(self):
        """缓存/推理字段为零时不写入序列化结果"""
        u = TokenUsage(prompt_tokens=1, completion_tokens=2, total_tokens=3, call_count=1)
        d = u.to_dict()
        assert d == {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3, "call_count": 1}
        assert "cache_hit_tokens" not in d
        assert "reasoning_tokens" not in d

    def test_to_dict_includes_optional_fields_when_present(self):
        u = TokenUsage(
            prompt_tokens=1,
            completion_tokens=2,
            total_tokens=3,
            call_count=1,
            cache_hit_tokens=4,
            cache_miss_tokens=5,
            reasoning_tokens=6,
        )
        d = u.to_dict()
        assert d["cache_hit_tokens"] == 4
        assert d["cache_miss_tokens"] == 5
        assert d["reasoning_tokens"] == 6

    def test_from_dict_roundtrip(self):
        """to_dict → from_dict 无损往返"""
        u = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            call_count=3,
            cache_hit_tokens=30,
            cache_miss_tokens=70,
            reasoning_tokens=10,
        )
        restored = TokenUsage.from_dict(u.to_dict())
        assert restored == u

    def test_from_dict_missing_keys_default_zero(self):
        """缺字段时默认为 0"""
        u = TokenUsage.from_dict({})
        assert u == TokenUsage()

    def test_str_when_no_calls(self):
        u = TokenUsage()
        assert str(u) == "N/A"
        assert u.short_str == "N/A"

    def test_str_formatted(self):
        u = TokenUsage(prompt_tokens=1000, completion_tokens=500, total_tokens=1500, call_count=2)
        s = str(u)
        assert "1,000" in s
        assert "500" in s
        assert "×2" in s

    def test_short_str_with_cache(self):
        u = TokenUsage(
            prompt_tokens=100,
            completion_tokens=50,
            total_tokens=150,
            call_count=1,
            cache_hit_tokens=80,
            cache_miss_tokens=20,
        )
        s = u.short_str
        assert "150" in s
        assert "cache:80%" in s
        assert "×1" in s


class TestTokenTracker:
    def test_accumulate_none_ignored(self):
        """usage=None 直接跳过，不产生调用记录"""
        t = TokenTracker()
        t.accumulate(None)
        assert t.total.call_count == 0

    def test_accumulate_total_zero_skipped(self):
        """total_tokens=0 视为无效数据跳过"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})
        assert t.total.call_count == 0

    def test_accumulate_standard_usage(self):
        """标准 OpenAI 结构"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, model="gpt-4o")
        assert t.total.prompt_tokens == 10
        assert t.total.completion_tokens == 5
        assert t.total.total_tokens == 15
        assert t.total.call_count == 1

    def test_accumulate_deepseek_nested_cache(self):
        """DeepSeek 嵌套 prompt_tokens_details.cached_tokens"""
        t = TokenTracker()
        t.accumulate({
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 60},
        })
        assert t.total.cache_hit_tokens == 60
        # miss 缺失时用 prompt - hit 推算
        assert t.total.cache_miss_tokens == 40

    def test_accumulate_flat_cache_fields(self):
        """平铺 prompt_cache_hit_tokens / prompt_cache_miss_tokens"""
        t = TokenTracker()
        t.accumulate({
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_cache_hit_tokens": 70,
            "prompt_cache_miss_tokens": 30,
        })
        assert t.total.cache_hit_tokens == 70
        assert t.total.cache_miss_tokens == 30

    def test_accumulate_nested_cache_preferred_over_flat(self):
        """嵌套结构优先；nested 有值时 flat 不覆盖"""
        t = TokenTracker()
        t.accumulate({
            "prompt_tokens": 100,
            "completion_tokens": 10,
            "total_tokens": 110,
            "prompt_tokens_details": {"cached_tokens": 60},
            "prompt_cache_hit_tokens": 90,
            "prompt_cache_miss_tokens": 10,
        })
        assert t.total.cache_hit_tokens == 60
        # flat miss 存在时直接采用（不触发 prompt - hit 推算）
        assert t.total.cache_miss_tokens == 10

    def test_accumulate_reasoning_tokens(self):
        """推理 token：嵌套 completion_tokens_details + 平铺兼容"""
        t = TokenTracker()
        t.accumulate({
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "completion_tokens_details": {"reasoning_tokens": 12},
        })
        assert t.total.reasoning_tokens == 12

        t2 = TokenTracker()
        t2.accumulate({
            "prompt_tokens": 10,
            "completion_tokens": 20,
            "total_tokens": 30,
            "reasoning_tokens": 15,
        })
        assert t2.total.reasoning_tokens == 15

    def test_accumulate_multiple_calls(self):
        """多次累加正确求和"""
        t = TokenTracker()
        for _ in range(3):
            t.accumulate({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15})
        assert t.total.prompt_tokens == 30
        assert t.total.completion_tokens == 15
        assert t.total.call_count == 3

    def test_accumulate_model_aggregation(self):
        """按模型分组累计"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, model="A")
        t.accumulate({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, model="A")
        t.accumulate({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model="B")

        assert t.for_model("A").call_count == 2
        assert t.for_model("B").call_count == 1
        assert t.for_model("unknown").call_count == 0  # 不存在的模型返回空
        assert set(t.models) == {"A", "B"}

    def test_to_dict_empty(self):
        """空 tracker 序列化只有 total 无 per_model"""
        t = TokenTracker()
        d = t.to_dict()
        assert d["total"]["call_count"] == 0
        assert "per_model" not in d

    def test_from_dict_none_returns_empty(self):
        t = TokenTracker.from_dict(None)
        assert t.total.call_count == 0
        assert t.models == []

    def test_from_dict_roundtrip(self):
        """to_dict → from_dict 完整往返（含 per_model）"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}, model="gpt")
        t.accumulate({"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}, model="deepseek")

        restored = TokenTracker.from_dict(t.to_dict())
        assert restored.total == t.total
        assert restored.for_model("gpt") == t.for_model("gpt")
        assert restored.for_model("deepseek") == t.for_model("deepseek")

    def test_format_detailed_markdown(self):
        """format_detailed() 输出 markdown 表格"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}, model="gpt")
        s = t.format_detailed(current_model="gpt")
        assert "📊 **Token 消耗统计**" in s
        assert "当前模型: `gpt`" in s
        assert "调用次数" in s
        assert "| **总计** | **150** |" in s

    def test_format_detailed_skips_unknown_current_model(self):
        """current_model 不在记录中时不单独展示"""
        t = TokenTracker()
        t.accumulate({"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}, model="gpt")
        s = t.format_detailed(current_model="nonexistent")
        assert "当前模型" not in s
        assert "模型: `gpt`" in s

    def test_str_uses_format_detailed(self):
        t = TokenTracker()
        assert str(t) == t.format_detailed()
