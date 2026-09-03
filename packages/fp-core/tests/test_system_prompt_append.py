"""
system_prompt_append 扩展点测试

覆盖：
1. apply_system_prompt_append helper：str / list[str] / None / 空白过滤 / 空行拼接
2. ensure_initialized 接线：ON_INIT 插件通过 system_prompt_append 注入，
   支持多插件 list 收集与旧 str 覆盖式向后兼容
"""

from __future__ import annotations

import asyncio
import os
import sys
from typing import TYPE_CHECKING

import pytest

# ── 确保 src 在 sys.path ──
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from fp_core.core.conversation import ConversationState
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.prompts import apply_system_prompt_append

if TYPE_CHECKING:
    from fp_core.core.agent import Agent

BASE_PROMPT = "你是 FP Agent，一个通用问题解决者。"


# ═══════════════════════════════════════════════════════════════
#  helper 单测
# ═══════════════════════════════════════════════════════════════


def _conv() -> ConversationState:
    return ConversationState(BASE_PROMPT)


def test_append_none_noop():
    c = _conv()
    apply_system_prompt_append(c, None)
    assert c.system_prompt == BASE_PROMPT


def test_append_str_once():
    c = _conv()
    apply_system_prompt_append(c, "扩展段落 A")
    assert c.system_prompt == f"{BASE_PROMPT}\n\n扩展段落 A"


def test_append_list_collects_all():
    c = _conv()
    apply_system_prompt_append(c, ["扩展段落 A", "扩展段落 B"])
    assert c.system_prompt == f"{BASE_PROMPT}\n\n扩展段落 A\n\n扩展段落 B"


def test_append_empty_list_noop():
    c = _conv()
    apply_system_prompt_append(c, [])
    assert c.system_prompt == BASE_PROMPT


def test_append_filters_blank_parts():
    c = _conv()
    apply_system_prompt_append(c, ["  段落 A  ", "", "  ", "段落 B"])
    assert c.system_prompt == f"{BASE_PROMPT}\n\n段落 A\n\n段落 B"


def test_append_strip_edges():
    c = _conv()
    apply_system_prompt_append(c, ["  段落 A  "])
    assert c.system_prompt == f"{BASE_PROMPT}\n\n段落 A"


# ═══════════════════════════════════════════════════════════════
#  ensure_initialized 接线（stub Agent，不触发完整 __init__）
# ═══════════════════════════════════════════════════════════════


def _make_stub_agent() -> Agent:
    """构造最小 Agent 实例：只具备 ensure_initialized 所需字段"""
    from fp_core.core.agent import Agent

    agent = object.__new__(Agent)  # 跳过 __init__（无需 LLM 配置/会话目录）
    agent._init_lock = asyncio.Lock()
    agent._initialized = False
    agent._conv = ConversationState(BASE_PROMPT)
    agent.lifecycle = LifecycleManager()

    class _ToolExec:
        registry = None

    agent._tool_exec = _ToolExec()
    agent.state = None
    return agent


@pytest.mark.asyncio
async def test_ensure_initialized_multiple_plugins_list_collection():
    """多插件各自 setdefault().append() → 全部注入 system prompt（不互相覆盖）"""
    agent = _make_stub_agent()

    async def plugin_a(ctx, **kwargs):
        ctx.data.setdefault("system_prompt_append", []).append("【插件 A】说明")
        return ctx

    async def plugin_b(ctx, **kwargs):
        ctx.data.setdefault("system_prompt_append", []).append("【插件 B】说明")
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_INIT, plugin_a, name="a", priority=10)
    agent.lifecycle.register(LifecycleHook.ON_INIT, plugin_b, name="b", priority=20)

    await agent.ensure_initialized()

    prompt = agent._conv.system_prompt
    assert prompt.startswith(BASE_PROMPT)
    assert "【插件 A】说明" in prompt
    assert "【插件 B】说明" in prompt
    # 两个插件都注入且不丢失（旧覆盖式会丢一个）
    assert "插件 A" in prompt and "插件 B" in prompt


@pytest.mark.asyncio
async def test_ensure_initialized_legacy_str_still_works():
    """旧插件直接写 str 覆盖式 → 向后兼容仍注入"""
    agent = _make_stub_agent()

    async def legacy_plugin(ctx, **kwargs):
        ctx.data["system_prompt_append"] = "【旧插件】说明"
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_INIT, legacy_plugin, name="legacy")

    await agent.ensure_initialized()
    assert "【旧插件】说明" in agent._conv.system_prompt


@pytest.mark.asyncio
async def test_ensure_initialized_idempotent_no_duplicate_append():
    """重复调用 ensure_initialized → 不重复追加"""
    agent = _make_stub_agent()

    async def plugin_a(ctx, **kwargs):
        ctx.data.setdefault("system_prompt_append", []).append("【插件 A】说明")
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_INIT, plugin_a, name="a")

    await agent.ensure_initialized()
    await agent.ensure_initialized()

    prompt = agent._conv.system_prompt
    assert prompt.count("【插件 A】说明") == 1
