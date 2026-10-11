"""platform_utils — 跨平台原语的最小验证。

聚焦 handoff 的 /proc 兜底：macOS / Windows 无 procfs，
``perform_exec_reload`` 必须给出可操作的诊断，而非无谓的 ``open()`` 失败。
"""

import os
from unittest.mock import MagicMock

import pytest

from fp_core import platform_utils
from fp_core.core import handoff


class TestHasProcfs:
    def test_reflects_runtime_probe(self):
        """has_procfs() 应等于运行期探测结果（Linux=True，macOS/Windows=False）。"""
        assert platform_utils.has_procfs() == os.path.isdir("/proc/self")

    def test_true_on_linux(self):
        """Linux 上恒为 True（CI ubuntu-latest / macos-latest 各验其位）。"""
        if platform_utils.is_linux():
            assert platform_utils.has_procfs() is True


class TestExecReloadNoProcfs:
    """无 procfs 平台：缺 FP_LAUNCH_JSON 时不许崩，要给可操作诊断。"""

    @pytest.mark.asyncio
    async def test_returns_actionable_diagnostic(self, monkeypatch):
        monkeypatch.delenv("FP_LAUNCH_JSON", raising=False)
        monkeypatch.setattr(platform_utils, "has_procfs", lambda: False)

        state = MagicMock()
        msg = await handoff.perform_exec_reload(state, kind="command")

        assert isinstance(msg, str) and msg.startswith("❌")
        assert "/proc/self" in msg
        assert "capture_launch_command" in msg
