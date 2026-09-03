"""测试 config 模块的 LLM 供应商/模型两级结构。

覆盖：旧格式规范化迁移、三级参数合并、ACTIVE_LLM 解析、同名模型消歧、
set_active_llm_state 内存态同步。纯逻辑，无网络依赖。

背景：旧版 LLM_PROVIDERS 是一维 key（每个 value 直接带 model 字段），
导致「一供应商多模型 → 复制 key」与「多供应商同名模型 → 合成 key 消歧」。
新版为 provider/models 两级命名空间 + ACTIVE_LLM="provider/model" 引用。
"""

import os

# 要在 import config 之前设环境变量，防止 config 模块加载时读取真实配置
os.environ.setdefault("XDG_CONFIG_HOME", "/tmp/fp_test_config")
os.environ.setdefault("XDG_DATA_HOME", "/tmp/fp_test_data")

from fp_core import config  # noqa: E402


def _reset(**overrides):
    """设置 _json_cfg 为干净的测试配置。"""
    base = {
        "LLM_API_KEY": "sk-top",
        "LLM_API_BASE_URL": "https://top.example.com/v1",
        "LLM_MODEL": "top-model",
        "TEMPERATURE": 0.8,
        "MAX_TOKENS": 32768,
        "TIMEOUT": 300,
        "RETRY_COUNT": 3,
    }
    base.update(overrides)
    config._json_cfg = base


class TestNormalizeProviders:
    def test_legacy_flat_wrapped(self):
        """旧扁平格式 → 两级结构：model 下沉为 models.{model}，生成参数随行"""
        raw = {
            "aliyun-qwen": {
                "api_key": "k1",
                "base_url": "https://a.example.com/v1",
                "model": "qwen3.7-plus",
                "temperature": 0.3,
            },
            "amd": {"api_key": "k2", "base_url": "https://b.example.com/v1", "model": "DeepSeek-V4"},
        }
        norm = config.normalize_providers(raw)
        assert norm["aliyun-qwen"]["api_key"] == "k1"
        assert norm["aliyun-qwen"]["base_url"] == "https://a.example.com/v1"
        # temperature 下沉为模型级差异，不再留在 provider 级
        assert "temperature" not in norm["aliyun-qwen"]
        assert norm["aliyun-qwen"]["models"] == {"qwen3.7-plus": {"temperature": 0.3}}
        assert norm["amd"]["models"] == {"DeepSeek-V4": {}}

    def test_new_two_level_passthrough(self):
        """新格式原样保留"""
        raw = {
            "openai": {
                "api_key": "k1",
                "base_url": "https://openai.example.com/v1",
                "models": {"gpt-4o": {"temperature": 0.1}, "gpt-4o-mini": {}},
            }
        }
        norm = config.normalize_providers(raw)
        assert set(norm["openai"]["models"]) == {"gpt-4o", "gpt-4o-mini"}
        assert norm["openai"]["models"]["gpt-4o"]["temperature"] == 0.1

    def test_same_model_name_across_providers_kept_separate(self):
        """不同供应商同名模型：各自独立叶子，互不覆盖"""
        raw = {
            "openai": {"api_key": "a", "base_url": "u1", "models": {"gpt-4o": {"temperature": 0.1}}},
            "azure": {"api_key": "b", "base_url": "u2", "models": {"gpt-4o": {"temperature": 0.7}}},
        }
        norm = config.normalize_providers(raw)
        assert norm["openai"]["models"]["gpt-4o"]["temperature"] == 0.1
        assert norm["azure"]["models"]["gpt-4o"]["temperature"] == 0.7

    def test_junk_dropped(self):
        """非 dict / 无 model / 名字含斜杠 → 丢弃"""
        raw = {
            "bad1": "not-a-dict",
            "bad2": {"api_key": "no-model"},
            "bad/slash": {"api_key": "k", "base_url": "u", "models": {"m": {}}},
            "good": {"api_key": "k", "base_url": "u", "models": {"m": {}}},
        }
        norm = config.normalize_providers(raw)
        assert set(norm) == {"good"}

    def test_empty_or_non_dict(self):
        assert config.normalize_providers(None) == {}
        assert config.normalize_providers([]) == {}
        assert config.normalize_providers({"p": {"api_key": "k", "base_url": "u", "models": {}}}) == {}


class TestResolveParams:
    def _setup_two_level(self):
        _reset(
            LLM_PROVIDERS={
                "deepseek": {
                    "api_key": "sk-ds",
                    "base_url": "https://api.deepseek.com/v1",
                    "timeout": 60,
                    "models": {
                        "deepseek-v4-flash": {},
                        "reasoner": {"temperature": 0.1, "extra_body": {"enable_thinking": True}},
                    },
                },
                "openai": {
                    "api_key": "sk-oa",
                    "base_url": "https://api.openai.com/v1",
                    "models": {"gpt-4o": {"max_tokens": 1000}},
                },
            }
        )

    def test_global_temperature_fallback(self):
        """model 无配置 → provider 无配置 → 顶层 TEMPERATURE"""
        self._setup_two_level()
        r = config.resolve_llm_params("deepseek", "deepseek-v4-flash")
        assert r is not None
        assert r["api_key"] == "sk-ds"
        assert r["temperature"] == 0.8  # 顶层
        assert r["timeout"] == 60  # provider 级
        assert r["extra_body"] == {"enable_thinking": False}  # 默认

    def test_model_level_override(self):
        """模型级 temperature/extra_body 覆盖"""
        self._setup_two_level()
        r = config.resolve_llm_params("deepseek", "reasoner")
        assert r["temperature"] == 0.1
        assert r["extra_body"] == {"enable_thinking": True}

    def test_provider_level_extra_body_merged_with_model(self):
        """extra_body 浅合并：provider 打底，model 覆盖同名键、保留其余"""
        _reset(
            LLM_PROVIDERS={
                "p": {
                    "api_key": "k",
                    "base_url": "u",
                    "extra_body": {"enable_thinking": False, "max_retries": 2},
                    "models": {"m": {"extra_body": {"enable_thinking": True}}},
                }
            }
        )
        r = config.resolve_llm_params("p", "m")
        assert r is not None
        assert r["extra_body"] == {"enable_thinking": True, "max_retries": 2}

    def test_missing_provider_model(self):
        self._setup_two_level()
        assert config.resolve_llm_params("nope", "x") is None
        assert config.resolve_llm_params("deepseek", "nope") is None

    def test_same_model_different_provider_resolves_independently(self):
        """同名模型 gpt-4o 在 openai/azure 下解析出各自 key/参数"""
        _reset(
            LLM_PROVIDERS={
                "openai": {"api_key": "a", "base_url": "u1", "models": {"gpt-4o": {"temperature": 0.1}}},
                "azure": {"api_key": "b", "base_url": "u2", "models": {"gpt-4o": {"temperature": 0.7}}},
            }
        )
        r1 = config.resolve_llm_params("openai", "gpt-4o")
        r2 = config.resolve_llm_params("azure", "gpt-4o")
        assert r1 is not None and r2 is not None
        assert r1["api_key"] == "a" and r2["api_key"] == "b"
        assert r1["temperature"] == 0.1 and r2["temperature"] == 0.7


class TestActiveResolution:
    def _setup(self):
        _reset(
            ACTIVE_LLM="deepseek/reasoner",
            LLM_PROVIDERS={
                "deepseek": {
                    "api_key": "sk-ds",
                    "base_url": "https://api.deepseek.com/v1",
                    "models": {"deepseek-v4-flash": {}, "reasoner": {"temperature": 0.1}},
                }
            },
        )

    def test_active_key_preferred(self):
        self._setup()
        r = config.resolve_active_llm()
        assert r is not None
        assert r["provider"] == "deepseek"
        assert r["model"] == "reasoner"
        assert r["temperature"] == 0.1

    def test_infer_from_top_level_when_active_missing(self):
        """缺 ACTIVE_LLM → 顶层三键推导（旧配置兼容路径）"""
        _reset(
            LLM_MODEL="reasoner",
            LLM_API_KEY="sk-ds",
            LLM_API_BASE_URL="https://api.deepseek.com/v1",
            LLM_PROVIDERS={
                "deepseek": {
                    "api_key": "sk-ds",
                    "base_url": "https://api.deepseek.com/v1",
                    "models": {"deepseek-v4-flash": {}, "reasoner": {}},
                }
            },
        )
        r = config.resolve_active_llm()
        assert r is not None
        assert r["provider"] == "deepseek"
        assert r["model"] == "reasoner"

    def test_invalid_active_falls_back_to_infer(self):
        """ACTIVE_LLM 指向表外 → 退化到顶层推导"""
        _reset(
            ACTIVE_LLM="nope/x",
            LLM_MODEL="deepseek-v4-flash",
            LLM_API_KEY="sk-ds",
            LLM_API_BASE_URL="https://api.deepseek.com/v1",
            LLM_PROVIDERS={
                "deepseek": {
                    "api_key": "sk-ds",
                    "base_url": "https://api.deepseek.com/v1",
                    "models": {"deepseek-v4-flash": {}, "reasoner": {}},
                }
            },
        )
        r = config.resolve_active_llm()
        assert r is not None
        assert r["model"] == "deepseek-v4-flash"

    def test_no_providers_table_returns_none(self):
        """无表 → None（调用方走顶层直连逻辑）"""
        _reset()
        assert config.resolve_active_llm() is None

    def test_top_level_same_model_ambiguous_resolves_by_endpoint(self):
        """顶层 model 同名但 base_url/key 不同 → 仍能唯一定位到匹配 provider"""
        _reset(
            LLM_MODEL="gpt-4o",
            LLM_API_KEY="b",
            LLM_API_BASE_URL="https://azure.example.com/v1",
            LLM_PROVIDERS={
                "openai": {"api_key": "a", "base_url": "https://openai.example.com/v1", "models": {"gpt-4o": {}}},
                "azure": {"api_key": "b", "base_url": "https://azure.example.com/v1", "models": {"gpt-4o": {}}},
            },
        )
        r = config.resolve_active_llm()
        assert r is not None
        assert r["provider"] == "azure"
        assert r["api_key"] == "b"

    def test_top_level_no_match_returns_none(self):
        """顶层三键与表内任何 provider 都不匹配 → 返回 None（顶层直连兜底）"""
        _reset(
            LLM_MODEL="gpt-4o",
            LLM_API_KEY="b",
            LLM_API_BASE_URL="https://azure.example.com/v1",
            LLM_PROVIDERS={
                "openai": {"api_key": "a", "base_url": "https://openai.example.com/v1", "models": {"gpt-4o": {}}},
            },
        )
        assert config.resolve_active_llm() is None


class TestActiveStateSync:
    def test_set_active_llm_state_updates_module(self):
        """热切换后模块内存态同步：常量与 _json_cfg 均更新"""
        _reset()
        config.set_active_llm_state(
            provider="aliyun",
            model="qwen-max",
            api_key="sk-ali",
            base_url="https://ali.example.com/v1",
            temperature=0.2,
            max_tokens=4096,
            extra_body={"enable_thinking": True},
            timeout=120,
            retry_count=5,
        )
        assert config.LLM_ACTIVE_ID == "aliyun/qwen-max"
        assert config.LLM_MODEL == "qwen-max"
        assert config.LLM_API_KEY == "sk-ali"
        assert config.LLM_TEMPERATURE == 0.2
        assert config.LLM_EXTRA_BODY == {"enable_thinking": True}
        assert config._json_cfg["ACTIVE_LLM"] == "aliyun/qwen-max"
        assert config._json_cfg["LLM_MODEL"] == "qwen-max"

    def test_infer_active_from_top_pure(self):
        provs = {"p1": {"models": {"m": {}}}, "p2": {"models": {"m": {}}}}
        assert config.infer_active_from_top(provs, "m", "", "k2") is None  # key 无命中
        assert config.infer_active_from_top(provs, "x", "", "") is None
