"""测试 config 模块 — 纯逻辑，无网络依赖"""

import os
import tempfile

# 要在 import config 之前设环境变量，防止 config 模块加载时读取真实配置
# 配置/数据目录隔离（跨平台）：POSIX 走 XDG_*，Windows 走 APPDATA/LOCALAPPDATA
# （XDG_* 在 Windows 被 platform_utils 忽略）。指向系统临时目录，测试不碰真实用户配置。
os.environ.setdefault("XDG_CONFIG_HOME", os.path.join(tempfile.gettempdir(), "fp_test_config"))
os.environ.setdefault("XDG_DATA_HOME", os.path.join(tempfile.gettempdir(), "fp_test_data"))
os.environ.setdefault("APPDATA", os.path.join(tempfile.gettempdir(), "fp_test_appdata"))
os.environ.setdefault("LOCALAPPDATA", os.path.join(tempfile.gettempdir(), "fp_test_localappdata"))

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
        assert "必填项未设置" in errors[0]

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


class TestRequiredKeys:
    """零默认语义：missing_llm_keys() 判定 + llm_setup_hint() 用户指引"""

    def test_all_missing(self, monkeypatch):
        for key in config._REQUIRED_LLM_KEYS:
            monkeypatch.setattr(config, key, "", raising=False)
        assert set(config.missing_llm_keys()) == set(config._REQUIRED_LLM_KEYS)

    def test_blank_string_counts_as_missing(self, monkeypatch):
        monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
        monkeypatch.setattr(config, "LLM_API_BASE_URL", "  ", raising=False)
        monkeypatch.setattr(config, "LLM_MODEL", "real-model", raising=False)
        missing = config.missing_llm_keys()
        assert "LLM_API_KEY" in missing
        assert "LLM_API_BASE_URL" in missing
        assert "LLM_MODEL" not in missing

    def test_all_set(self, monkeypatch):
        for key in config._REQUIRED_LLM_KEYS:
            monkeypatch.setattr(config, key, "x", raising=False)
        assert config.missing_llm_keys() == []

    def test_hint_mentions_path_keys_and_docs(self, monkeypatch):
        for key in config._REQUIRED_LLM_KEYS:
            monkeypatch.setattr(config, key, "", raising=False)
        hint = config.llm_setup_hint()
        assert config.USER_CONFIG_PATH in hint
        assert "fp docs" in hint
        for key in config._REQUIRED_LLM_KEYS:
            assert key in hint

    def test_no_placeholder_template_exists(self):
        """零默认：不再提供任何带占位值的 config.json 模板/初始化函数"""
        assert not hasattr(config, "get_default_config")
        assert not hasattr(config, "init_config")

    def test_quiet_check_returns_only_bool(self, monkeypatch, capsys):
        """quiet=True 只返回结果、不打印任何东西"""
        monkeypatch.setattr(config, "LLM_API_KEY", "", raising=False)
        assert config.check_llm_config(quiet=True) is False
        assert capsys.readouterr().out == ""


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
