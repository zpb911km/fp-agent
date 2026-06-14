"""测试 config 模块 — 纯逻辑，无网络依赖"""

import os
from unittest.mock import patch

# 要在 import config 之前设环境变量，防止 config 模块加载时读取真实配置
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/fp_test_config")
os.environ.setdefault("XDG_DATA_HOME", "/tmp/fp_test_data")

from fp_core import config


class TestValidateValue:
    """_validate_value() — 配置项级别验证"""

    def test_required_missing(self):
        """必填项缺失 → 返回错误"""
        errors = config._validate_value("LLM_API_KEY", None)
        assert len(errors) == 1
        assert "必填项未设置" in errors[0]

    def test_required_empty_string(self):
        """必填项为空字符串 → 返回错误"""
        errors = config._validate_value("LLM_API_KEY", "")
        assert len(errors) == 1
        assert "必填项未设置" in errors[0]

    def test_required_blank_string(self):
        """必填项为纯空格 → 返回错误"""
        errors = config._validate_value("LLM_API_KEY", "   ")
        assert len(errors) == 1

    def test_required_ok(self):
        """必填项正常填充 → 无错误"""
        errors = config._validate_value("LLM_API_KEY", "sk-test-key")
        assert len(errors) == 0

    def test_optional_none(self):
        """非必填项为 None → 无错误（跳过）"""
        errors = config._validate_value("TEMPERATURE", None)
        assert len(errors) == 0

    def test_type_mismatch(self):
        """类型错误 → 期望 str 实为 int"""
        errors = config._validate_value("LLM_MODEL", 123)
        assert len(errors) == 1
        assert "期望 str" in errors[0]

    def test_int_compatible_with_float(self):
        """int 可兼容 float 字段 → 无错误"""
        errors = config._validate_value("TEMPERATURE", 1)
        assert len(errors) == 0

    def test_float_out_of_range(self):
        """float 值超出范围 → 返回错误"""
        errors = config._validate_value("TEMPERATURE", 99.9)
        assert len(errors) == 1
        assert "超出有效范围" in errors[0]

    def test_float_below_range(self):
        """float 值低于范围 → 返回错误"""
        errors = config._validate_value("TEMPERATURE", -0.5)
        assert len(errors) == 1

    def test_float_at_boundary(self):
        """float 值在边界 → 无错误（边界值在范围内）"""
        errors = config._validate_value("TEMPERATURE", 0.0)
        assert len(errors) == 0
        errors = config._validate_value("TEMPERATURE", 2.0)
        assert len(errors) == 0

    def test_unknown_key(self):
        """未知字段 → 返回空列表"""
        errors = config._validate_value("NONEXISTENT_KEY", "anything")
        assert len(errors) == 0

    def test_max_tokens_out_of_range(self):
        """MAX_TOKENS 超出上限"""
        errors = config._validate_value("MAX_TOKENS", 999999)
        assert len(errors) == 1

    def test_retry_count_negative(self):
        """RETRY_COUNT 为负数"""
        errors = config._validate_value("RETRY_COUNT", -1)
        assert len(errors) == 1


class TestGetDefaultConfig:
    """get_default_config() — 默认配置模板"""

    def test_contains_required_keys(self):
        """返回的配置包含所有必填字段"""
        cfg = config.get_default_config()
        assert "LLM_API_KEY" in cfg
        assert "LLM_API_BASE_URL" in cfg
        assert "LLM_MODEL" in cfg

    def test_temperature_default(self):
        """TEMPERATURE 默认值为 0.8"""
        cfg = config.get_default_config()
        assert cfg["TEMPERATURE"] == 0.8

    def test_display_styles_present(self):
        """包含显示样式配置"""
        cfg = config.get_default_config()
        assert "display_styles" in cfg
        assert "info" in cfg["display_styles"]
        assert "error" in cfg["display_styles"]

    def test_is_valid_json(self):
        """返回的配置可以序列化为 JSON（无特殊对象）"""
        import json

        cfg = config.get_default_config()
        json.dumps(cfg)  # 不应抛出异常


class TestColorSupport:
    """color_supported() — 终端颜色检测"""

    @patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True)
    def test_force_color(self):
        """FORCE_COLOR 环境变量 → 返回 True"""
        assert config.color_supported() is True

    @patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True)
    def test_no_color(self):
        """NO_COLOR 环境变量 → 返回 False"""
        assert config.color_supported() is False


class TestDisplayStyle:
    """get_display_style() — 显示样式"""

    def test_unknown_style(self):
        """未知样式名 → 返回默认（无颜色、无样式）"""
        style = config.get_display_style("nonexistent")
        assert style["color"] == ""
        assert style["bold"] is False

    def test_default_styles_exist(self):
        """内置样式列表中各样式均有定义"""
        for name in ("info", "error", "warning", "llm_thought", "llm_tool"):
            style = config.get_display_style(name)
            assert isinstance(style, dict)
            assert "color" in style


class TestTruncate:
    """truncate() — 文本截断"""

    def test_no_truncation_needed(self):
        """短文本不截断"""
        result = config.truncate("short text", "info")
        # 无配置时默认 -1（不截断）
        assert result == "short text"

    def test_long_text_truncated(self):
        """长文本按配置截断"""
        # 临时覆盖 _json_cfg
        config._json_cfg = {
            "display_truncation": {"test_field": 10},
        }
        result = config.truncate("hello world this is long", "test_field")
        assert len(result) < len("hello world this is long")
        assert "…" in result
        assert "+" in result  # 包含超出的字符计数


class TestCheckLlmConfig:
    """check_llm_config() — LLM 配置完整性检查"""

    def test_missing_key(self):
        """API Key 缺失 → 返回 False"""
        # 模拟空配置
        with (
            patch.object(config, "LLM_API_KEY", ""),
            patch.object(config, "LLM_API_BASE_URL", "https://test.com"),
            patch.object(config, "LLM_MODEL", "test-model"),
        ):
            assert config.check_llm_config() is False


class TestValueFunction:
    """_value() — 三级优先级取值"""

    def test_json_over_env(self):
        """JSON 配置优先于环境变量"""
        config._json_cfg = {"LLM_MODEL": "from-json"}
        os.environ["LLM_MODEL"] = "from-env"
        # 注意：_value 是模块加载时调用的，这里只是验证逻辑
        # 实际 LLM_MODEL 模块变量在 import 时已确定
        # _value 本身可以被独立测试
        val = config._value("LLM_MODEL", "default")
        assert val == "from-json"

    def test_env_over_default(self):
        """环境变量优先于默认值"""
        config._json_cfg = {}  # JSON 中无此键
        os.environ["LANG_TEST"] = "from-env"
        val = config._value("LANG_TEST", "default")
        assert val == "from-env"

    def test_default_fallback(self):
        """均未设置 → 返回默认值"""
        key = "_TEST_NONEXISTENT_KEY_"
        if key in os.environ:
            del os.environ[key]
        config._json_cfg = {}
        val = config._value(key, "fallback")
        assert val == "fallback"
