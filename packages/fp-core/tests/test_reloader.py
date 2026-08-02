"""测试 AgentReloader — 热重载引擎

覆盖重点：
- reload_modules() 静态入口
- _reload_modules() 依赖顺序（子模块先于主模块）
- reload() 完整流程：before/after 回调、旧 Agent 关闭、会话保存/恢复、info
- 异常路径：模块重载失败、会话恢复失败（不阻塞）
"""

import importlib
import sys
import types
from unittest.mock import AsyncMock, MagicMock, patch

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


class TestReload:
    def _make_fake_agent(self, session_id="s_test"):
        """构造一个状态完整的假 Agent"""
        agent = MagicMock()
        agent.state.session.session_id = session_id
        agent.state.session.save_and_summarize = MagicMock()
        agent.state.session.switch_session = MagicMock(return_value=True)
        agent.state.session.load_context = MagicMock(return_value=[{"role": "user", "content": "旧消息"}])
        agent.state.conversation.to_serializable = MagicMock(return_value=[])
        agent.state.conversation.reset = MagicMock()
        agent.state.conversation.replace_all = MagicMock()
        agent.shutdown = AsyncMock()
        return agent

    def _make_new_agent_class(self, fail_switch=False, fail_register=False):
        """构造假的新 Agent 类，reload 内部会实例化它"""

        class FakeAgent:
            instances = []

            def __init__(self, enable_log=True, io=None, on_shutdown=None):
                self.enable_log = enable_log
                self.io = io
                self.on_shutdown = on_shutdown
                self.model = "fake-model"
                self.plugins = MagicMock()
                if fail_register:
                    self.plugins.register.side_effect = Exception("bad plugin")
                self.state = MagicMock()
                self.state.session = MagicMock()
                self.state.session.session_id = "s_new"
                if fail_switch:
                    self.state.session.switch_session = MagicMock(side_effect=Exception("恢复失败"))
                else:
                    self.state.session.switch_session = MagicMock(return_value=True)
                self.state.session.load_context = MagicMock(return_value=[{"role": "user", "content": "恢复的消息"}])
                self.state.conversation = MagicMock()
                self.ensure_initialized = AsyncMock()
                FakeAgent.instances.append(self)

        return FakeAgent

    @pytest.mark.asyncio
    async def test_reload_full_flow(self):
        """完整流程：before→保存→关闭→重载→新agent→恢复会话→after→info"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class()

        before_called = MagicMock()
        after_called = MagicMock()

        with (
            patch.object(reloader, "_reload_modules") as mock_reload,
            patch("fp_core.core.agent.Agent", fake_cls),
        ):
            new_agent, info = await AgentReloader.reload(
                old_agent,
                before_reload=before_called,
                after_reload=after_called,
            )

        # 回调执行
        before_called.assert_called_once()
        after_called.assert_called_once_with(new_agent)

        # 旧会话保存 + 关闭
        old_agent.state.session.save_and_summarize.assert_called_once()
        old_agent.shutdown.assert_awaited_once()
        assert old_agent.state.silent_shutdown is True

        # 模块重载
        mock_reload.assert_called_once()

        # 新 agent 静默创建
        assert new_agent.enable_log is False
        assert new_agent.on_shutdown is None

        # 会话恢复
        new_agent.state.session.switch_session.assert_called_once_with("s_test")
        new_agent.state.conversation.replace_all.assert_called_once()

        # info
        assert info["session_id"] == "s_new"
        assert info["model"] == "fake-model"
        assert info["session_restored"] is True

    @pytest.mark.asyncio
    async def test_reload_async_callbacks(self):
        """异步 before/after 回调被 await"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class()
        before_async = AsyncMock()
        after_async = AsyncMock()

        with (
            patch.object(reloader, "_reload_modules"),
            patch("fp_core.core.agent.Agent", fake_cls),
        ):
            await AgentReloader.reload(old_agent, before_reload=before_async, after_reload=after_async)

        before_async.assert_awaited_once()
        after_async.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_reload_runtime_error_propagates(self):
        """模块重载失败 → RuntimeError 上抛，不创建新 agent"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class()

        with (
            patch.object(reloader, "_reload_modules", side_effect=RuntimeError("重载失败")),
            patch("fp_core.core.agent.Agent", fake_cls),
            pytest.raises(RuntimeError, match="重载失败"),
        ):
            await AgentReloader.reload(old_agent)

        assert len(fake_cls.instances) == 0

    @pytest.mark.asyncio
    async def test_reload_session_restore_failure_does_not_block(self):
        """会话恢复失败只记 warning，不抛异常"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class(fail_switch=True)

        with (
            patch.object(reloader, "_reload_modules"),
            patch("fp_core.core.agent.Agent", fake_cls),
            patch("fp_core.core.reloader.get_logger") as mock_logger,
        ):
            new_agent, info = await AgentReloader.reload(old_agent)

        assert info["session_restored"] is False
        assert info["session_id"] == "s_new"
        mock_logger.return_value.warning.assert_called()

    @pytest.mark.asyncio
    async def test_reload_extra_plugins_registered(self):
        """extra_plugins 注册到新 agent"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class()
        plugin = MagicMock()
        plugin.name = "p1"

        with (
            patch.object(reloader, "_reload_modules"),
            patch("fp_core.core.agent.Agent", fake_cls),
        ):
            new_agent, _ = await AgentReloader.reload(old_agent, extra_plugins=[plugin])

        new_agent.plugins.register.assert_called_once_with(plugin)

    @pytest.mark.asyncio
    async def test_reload_plugin_register_failure_tolerated(self):
        """插件注册失败只 warning，不阻塞整体"""
        old_agent = self._make_fake_agent()
        fake_cls = self._make_new_agent_class(fail_register=True)
        plugin = MagicMock()
        plugin.name = "bad"

        with (
            patch.object(reloader, "_reload_modules"),
            patch("fp_core.core.agent.Agent", fake_cls),
            patch("fp_core.core.reloader.get_logger") as mock_logger,
        ):
            new_agent, info = await AgentReloader.reload(old_agent, extra_plugins=[plugin])

        # 注册被调用且抛异常，但整体成功
        new_agent.plugins.register.assert_called_once_with(plugin)
        mock_logger.return_value.warning.assert_called()
        assert info["session_id"] == "s_new"
