"""测试 AgentReloader — 模块重载引擎

覆盖重点：
- reload_modules() 静态入口
- _reload_modules() 依赖顺序（子模块先于主模块）
- reload() 已废弃路径的防复活断言

历史：AgentReloader.reload 原地重建路径（shutdown 旧 Agent → importlib.reload →
新建）已删除——失败时会留下已 shutdown 的僵尸 Agent（自杀窗口），与 reload
门禁协议的救火原则冲突。进程级热重启统一走 fp_core.core.handoff.perform_exec_reload。
原 TestReload 类（before/after 回调、会话保存恢复、info 构造）随该路径一并移除。
"""

import importlib
import sys
import types
from unittest.mock import patch

import pytest

from fp_core.core import reloader
from fp_core.core.reloader import AgentReloader


def _fake_module(name: str):
    """构造带 __name__ 的假模块对象（MagicMock 不支持 __name__）"""
    return types.SimpleNamespace(__name__=name)


class TestReloadModules:
    def test_reload_modules_static_calls_internal(self):
        """静态入口委托给 _reload_modules"""
        with patch.object(reloader, "_reload_modules") as mock_internal:
            AgentReloader.reload_modules()
            mock_internal.assert_called_once()

    def test_submodules_reload_before_main_modules(self):
        """子模块先于主模块 reload，且主模块按 _RELOAD_MODULES 顺序"""
        fake_modules = {
            name: _fake_module(name)
            for name in (
                "fp_core",
                "fp_core.config",
                "fp_core.core.io",
                "fp_core.core.lifecycle",
                "fp_core.core.session",
                "fp_core.core.llm_client",
                "fp_core.plugins.base.plugin",
                "fp_core.prompts.agent",
                "fp_core.commands",
                "fp_core.tools",
                "fp_core.core.agent",
                # 子模块（应在前一轮 reload）
                "fp_core.commands.reload",
                "fp_core.tools.core",
                "fp_core.core.tool_executor",
                "fp_core.core.conversation",
            )
        }
        reloaded: list[str] = []

        def fake_reload(mod):
            reloaded.append(mod.__name__)
            return mod

        with (
            patch.dict(sys.modules, fake_modules, clear=False),
            patch.object(importlib, "reload", side_effect=fake_reload),
        ):
            reloader._reload_modules()

        # 子模块出现在所有主模块之前
        sub_indices = [
            reloaded.index(n) for n in ("fp_core.commands.reload", "fp_core.tools.core", "fp_core.core.tool_executor")
        ]
        main_indices = [reloaded.index(n) for n in ("fp_core.config", "fp_core.core.io", "fp_core.core.agent")]
        assert max(sub_indices) < min(main_indices)

        # 主模块按依赖顺序
        assert reloaded.index("fp_core.config") < reloaded.index("fp_core.core.io")
        assert reloaded.index("fp_core.core.llm_client") < reloaded.index("fp_core.core.agent")

    def test_reload_main_module_error_raises(self):
        """主模块 reload 失败 → RuntimeError"""
        fake_modules = {
            "fp_core": _fake_module("fp_core"),
            "fp_core.config": _fake_module("fp_core.config"),
        }

        def failing_reload(mod):
            if mod.__name__ == "fp_core.config":
                raise ImportError("boom")
            return mod

        with (
            patch.dict(sys.modules, fake_modules, clear=False),
            patch.object(importlib, "reload", side_effect=failing_reload),
            pytest.raises(RuntimeError, match="重载模块 fp_core.config 失败"),
        ):
            reloader._reload_modules()


class TestAntiRevival:
    def test_inplace_agent_reload_path_stays_removed(self):
        """防复活断言：原地重建路径（自杀窗口）不许回来。

        进程级热重启统一走 fp_core.core.handoff.perform_exec_reload；
        若此断言变红，说明有人把已删除的 AgentReloader.reload 加了回来，
        必须先回答"失败时旧 Agent 已 shutdown 成僵尸"这一问题。
        """
        assert not hasattr(AgentReloader, "reload")
        assert hasattr(AgentReloader, "reload_modules")
