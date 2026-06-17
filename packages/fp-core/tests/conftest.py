"""pytest fixtures — 隔离文件 I/O，避免污染真实配置"""

import tempfile
from collections.abc import Generator

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


@pytest.fixture
def temp_sessions_dir() -> Generator[str, None, None]:
    """创建临时会话目录，覆盖 config.SESSIONS_DIR。

    使用方式:
        import fp_core.config as cfg
        cfg.SESSIONS_DIR = temp_sessions_dir
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        yield tmpdir
