"""插件注入工具/命令的对称清理测试

回归锚点：ToolRegistry 曾与 commands 注册表同病——只注册不注销。
task_system 的 4 工具、shortcircuit 的工具/命令在插件禁用（on_unregister）
后必须全部失效，否则残留工具仍可被 LLM 调用（execute 按 definition name 全表匹配）。
"""

import os
import sys
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import fp_core.commands as cmd_mod
from fp_core.commands import get_command
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.plugins.shortcircuit import command as sc_command
from fp_core.plugins.shortcircuit.plugin import ShortcircuitPlugin
from fp_core.plugins.task_system import TaskSystemPlugin
from fp_core.tools import ToolRegistry


def _empty_registry() -> ToolRegistry:
    """空 ToolRegistry：只保留 core，清掉自动扫描的插件"""
    reg = ToolRegistry()
    reg._plugins.clear()
    return reg


def _snapshot_commands():
    return (dict(cmd_mod._commands), set(cmd_mod._dynamic_names))


def _restore_commands(snap):
    cmd_mod._commands, cmd_mod._dynamic_names = snap


def _tool_names(reg: ToolRegistry):
    return {d["function"]["name"] for d in reg.get_all_definitions()}


# ═══════════════════════════════════════════════════════════
# ToolRegistry.unregister_tool 语义
# ═══════════════════════════════════════════════════════════


def test_unregister_tool_basic_and_idempotent():
    from fp_core.plugins.task_system.tools import handle_create

    reg = _empty_registry()
    defn = {
        "type": "function",
        "function": {
            "name": "task_create",
            "description": "d",
            "parameters": {"type": "object", "properties": {}},
        },
    }
    reg.register_tool("task_create", defn, handle_create)  # type: ignore[arg-type]
    assert "task_create" in _tool_names(reg)

    reg.unregister_tool("task_create")
    assert "task_create" not in _tool_names(reg)

    reg.unregister_tool("task_create")  # 幂等：不抛异常
    reg.unregister_tool("never_registered")  # 幂等


# ═══════════════════════════════════════════════════════════
# 插件 on_unregister 对称清理
# ═══════════════════════════════════════════════════════════


async def _run_on_init(plugin, reg):
    """把插件挂到新 lifecycle 并触发 ON_INIT（复刻真实 Agent 注入路径）"""
    lifecycle = LifecycleManager()
    plugin.on_register(lifecycle)
    return await lifecycle.emit(
        LifecycleHook.ON_INIT,
        tool_registry=reg,
        state=MagicMock(),
    )


@pytest.mark.asyncio
async def test_task_system_on_unregister_removes_its_tools():
    reg = _empty_registry()
    plugin = TaskSystemPlugin()

    await _run_on_init(plugin, reg)
    assert {"task_create", "task_update", "task_list", "task_clear"} <= _tool_names(reg)

    plugin.on_unregister()

    after = _tool_names(reg)
    for n in ("task_create", "task_update", "task_list", "task_clear"):
        assert n not in after, f"{n} 应随插件卸载被清理"


@pytest.mark.asyncio
async def test_shortcircuit_on_unregister_removes_tool_and_command():
    reg = _empty_registry()
    plugin = ShortcircuitPlugin()
    snap = _snapshot_commands()
    try:
        await _run_on_init(plugin, reg)
        assert "shortcircuit" in _tool_names(reg)
        assert get_command("sc") is sc_command

        plugin.on_unregister()

        assert "shortcircuit" not in _tool_names(reg), "工具应随插件卸载被清理"
        assert get_command("sc") is None, "/sc 命令应随插件卸载被清理"
    finally:
        _restore_commands(snap)
