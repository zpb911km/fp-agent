"""pytest fixtures — 隔离文件 I/O，避免污染真实配置"""

import tempfile
from collections.abc import Generator
from unittest.mock import patch

import pytest

# ── config 模块级 LLM_* 常量的"出厂快照" ──────────────────────────
# conftest 在测试 import 前加载，此刻 config 未被任何 set_active_llm_state()
# 污染，可作为还原基准。_MISSING 标记出厂时未定义的常量（LLM_EXTRA_BODY
# 在无激活项配置时才定义），还原时需删除而非 setattr。
_LLM_CONST_NAMES = (
    "LLM_ACTIVE_ID",
    "LLM_API_KEY",
    "LLM_API_BASE_URL",
    "LLM_MODEL",
    "LLM_TEMPERATURE",
    "LLM_MAX_TOKENS",
    "LLM_EXTRA_BODY",
    "LLM_TIMEOUT",
    "LLM_RETRY_COUNT",
)
_MISSING = object()


def _snapshot_llm_constants(module) -> dict[str, object]:
    return {name: getattr(module, name, _MISSING) for name in _LLM_CONST_NAMES}


_LLM_FACTORY = _snapshot_llm_constants(__import__("fp_core.config", fromlist=["x"]))


@pytest.fixture(autouse=True)
def _restore_llm_constants_after_each():
    """每个测试后还原 config 模块级 LLM_* 常量到出厂值。

    背景：config.set_active_llm_state() 用 globals() 写 LLM_ACTIVE_ID /
    LLM_MODEL / LLM_API_KEY / LLM_EXTRA_BODY 等（/model 热切换入口），
    测试若调用过它（如 test_llm_providers），常量即被污染成"上次激活项"。
    若不还原，同进程后续测试读到的是上一个测试遗留的激活态——定时炸弹。

    _reset_config_cache 只清 _json_cfg（读取缓存），不触及模块常量，
    二者互补：前者管"文件配置缓存"，本 fixture 管"内存激活态"。
    """
    yield
    import fp_core.config as cfg

    for name, value in _LLM_FACTORY.items():
        if value is _MISSING:
            cfg.__dict__.pop(name, None)
        else:
            setattr(cfg, name, value)


@pytest.fixture(autouse=True)
def _reset_config_cache():
    """每个测试前重置 config 模块的 JSON 缓存，防止跨测试污染。

    config 模块在 import 时执行 _load_json_config() 并缓存到 _json_cfg。
    测试中修改环境变量或配置文件后，需要手动重置缓存。
    """
    import fp_core.config as cfg

    cfg._json_cfg = {}
    yield


@pytest.fixture(autouse=True)
def _isolate_sessions_dir(tmp_path) -> Generator[None, None, None]:
    """全局隔离会话目录：任何测试都不允许写真实用户会话数据。

    背景：Agent.__init__ 无条件创建 SessionManager 并写入
    config.SESSIONS_DIR（~/.local/share/fp/sessions/），此前
    test_agent_tool_parallel.py 未隔离导致真实会话目录被测试垃圾污染。

    同时 patch 两个引用点：
      - fp_core.config.SESSIONS_DIR（agent.py 里 os.makedirs 用的）
      - fp_core.core.session.SESSIONS_DIR（SessionManager 内部用的）
    """
    import fp_core.config as cfg
    import fp_core.core.session as session_mod

    tmp = str(tmp_path / "sessions")
    with (
        patch.object(cfg, "SESSIONS_DIR", tmp),
        patch.object(session_mod, "SESSIONS_DIR", tmp),
    ):
        yield


@pytest.fixture
def temp_sessions_dir() -> Generator[str, None, None]:
    """创建临时会话目录，覆盖 config.SESSIONS_DIR。

    使用方式:
        import fp_core.config as cfg
        cfg.SESSIONS_DIR = temp_sessions_dir
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir
