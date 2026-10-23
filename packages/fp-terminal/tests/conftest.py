"""测试隔离：任何用例都不得读写用户真实的 ``config.json``。

TUI 用例会真实启动 ``FPApp``（on_mount 里读键位配置、启动时自愈陈旧条目），
若不重定向配置目录，测试就会**改写开发者的用户配置**——已实际发生过：
陈旧键位清理逻辑一次测试跑下来，把 ``~/.config/fp/config.json`` 里的
``tui.keys`` 删掉了。配置目录必须在进程级屏障外。

**只设 env 不够**（见 ``docs/dev/测试.md`` § 隔离用户目录）：``XDG_CONFIG_HOME``
只在 Linux 生效，macOS 的 ``get_config_dir()`` 走 ``~/Library/Application Support``
且不读任何环境变量——只设 env 会在 macOS 上悄悄读写真实用户目录（#169 教训）。
所以这里既设 env（覆盖走 env 的实现），也直接钉住**路径来源函数**本身。
"""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def _isolate_config_dir(tmp_path, monkeypatch):  # noqa: ANN001, ANN202
    """把配置目录固定到临时目录，三平台一致。"""
    cfg = tmp_path / "cfg"
    cfg.mkdir()
    for var in ("XDG_CONFIG_HOME", "APPDATA", "LOCALAPPDATA"):
        monkeypatch.setenv(var, str(cfg))

    # ① 函数内 import 的调用点（style.py / display.py）经模块属性解析 → 打这里
    from fp_core import platform_utils

    monkeypatch.setattr(platform_utils, "get_config_dir", lambda: str(cfg), raising=True)

    # ② 模块级 `from ... import get_config_dir` 的本地绑定 → 得单独打
    from fp_cli.appearance import configio

    monkeypatch.setattr(configio, "get_config_dir", lambda: str(cfg), raising=True)
    return cfg
