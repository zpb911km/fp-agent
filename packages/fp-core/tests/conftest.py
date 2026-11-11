"""pytest fixtures — 隔离文件 I/O，避免污染真实配置"""

import importlib.util as _ilu
import os
import sys as _sys
import tempfile
from collections.abc import Generator
from pathlib import Path as _Path
from unittest.mock import patch

import pytest

# ── LLM key 兜底（必须先于下方 fp_core.config 的 import）────────────
# config 把 LLM_API_KEY 焊成模块级常量，取值 JSON > env > ""，在 import 那刻
# 定格。全新机器（CI runner）无 ~/.config/fp/config.json，若此处不设 env，
# 常量恒为 "" → Agent 构造 check_llm_config() 拒绝 → 70 个用例连环挂。
# 单包跑（rootdir=packages/fp-core）加载不到仓库根 conftest，故在此再兜一层。
os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-pytest")

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


# ── 会话/数据根隔离：实现在仓库根 conftest.py（#219 上提，单点维护）────────
# 为什么这里保留一段**再导出**而不是照搬删除：`pytest packages/fp-core/tests`
# （含 docs/dev/测试.md 的 pre-commit 门禁命令）rootdir=packages/fp-core，
# pytest 只从 rootdir 向下收集 conftest → 仓库根 conftest **不加载**；若本文件
# 也删掉隔离，单包跑会重新写真实 ~/.local/share/fp/sessions/（#219 溯源确认
# 的正是这类目录级缺口）。故按路径取根 conftest 的 fixture 对象导入本命名空间——
# 实现只有一份，这里只做转发，不复制实现。
_ROOT_CONF_PATH = _Path(__file__).resolve().parents[3] / "conftest.py"  # tests→fp-core→packages→仓库根


def _load_root_conftest():
    """取根 conftest 模块：已被 pytest 加载则复用，否则按路径加载一次。"""
    for _m in list(_sys.modules.values()):  # 不依赖模块名（各包 tests 可能同名）
        if _m is not None and _Path(getattr(_m, "__file__", "") or "").resolve() == _ROOT_CONF_PATH:
            return _m
    _spec = _ilu.spec_from_file_location("_fp_repo_root_conftest", _ROOT_CONF_PATH)
    assert _spec is not None and _spec.loader is not None, f"根 conftest 缺失: {_ROOT_CONF_PATH}"
    _mod = _ilu.module_from_spec(_spec)
    _sys.modules["_fp_repo_root_conftest"] = _mod
    _spec.loader.exec_module(_mod)
    return _mod


_root_conftest = _load_root_conftest()
_isolate_sessions_dir = _root_conftest._isolate_sessions_dir
_isolate_data_dir = _root_conftest._isolate_data_dir


@pytest.fixture
def temp_sessions_dir() -> Generator[str, None, None]:
    """创建临时会话目录，覆盖 config.SESSIONS_DIR。

    使用方式:
        import fp_core.config as cfg
        cfg.SESSIONS_DIR = temp_sessions_dir
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir


@pytest.fixture(autouse=True)
def _disable_zeta_network():
    """Zeta 插件默认不启用网络面——防测试污染 ~/.local/share/fp/peers 与 .fp/ 实例锁。"""
    with patch.dict(os.environ, {"FP_ZETA_DISABLE": "1"}):
        yield
