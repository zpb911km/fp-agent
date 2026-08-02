"""pytest fixtures — 隔离文件 I/O，避免污染真实配置"""

import tempfile
from collections.abc import Generator
from unittest.mock import patch

import pytest


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
