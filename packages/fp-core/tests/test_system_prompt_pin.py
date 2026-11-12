"""system prompt 固化（reload 缓存税）测试

覆盖：
1. 会话首次初始化 → system prompt 固化进 meta（且落盘）
2. resume（模拟 reload / 进程重启）→ 复用固化原文，而不是重新构建
3. 固化版本已含插件段 → 不重复 append（否则前缀被改写）
4. rebuild_system_prompt 默认复用固化版本；refresh=True 才重建并覆盖
5. clear_session_file 只清消息，保留固化（否则 /clear 会悄悄换前缀）
"""

from __future__ import annotations

import json
import os
import sys as _sys
from typing import Any

import pytest

_sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-prompt-pin")

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.agent import Agent
from fp_core.core.prompt_builder import PromptBuilder

PINNED = "【固化提示词】你是测试 Agent，版本 A。"
FRESH = "【新提示词】你是测试 Agent，版本 B。"


def _mk_env(tmp_path, monkeypatch) -> None:
    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))


def _patch_build(monkeypatch, text: str) -> None:
    monkeypatch.setattr(PromptBuilder, "build_system_prompt", lambda self: text)


@pytest.mark.asyncio
async def test_first_init_pins_prompt(tmp_path, monkeypatch):
    _mk_env(tmp_path, monkeypatch)
    _patch_build(monkeypatch, PINNED)

    agent = Agent(enable_log=False)
    assert agent.session.get_system_prompt() is None  # 构造时还没初始化
    await agent.ensure_initialized()

    assert agent._conv.system_prompt.strip().startswith(PINNED)
    assert agent.session.get_system_prompt() == agent._conv.system_prompt

    # 落盘：写一条消息后，meta 里应能看到固化原文
    agent.session.save_message("user", "hi")
    path = agent.session.get_session_path()
    with open(path, encoding="utf-8") as f:
        meta = json.loads(f.readline())
    assert meta[session_mod.SYSTEM_PROMPT_FIELD] == agent._conv.system_prompt
    assert meta.get(session_mod.SYSTEM_PROMPT_AT_FIELD)


@pytest.mark.asyncio
async def test_resume_reuses_pinned_prompt(tmp_path, monkeypatch):
    """模拟 reload：同一 sid 重启，代码/提示词已变 → 仍用固化那份。"""
    _mk_env(tmp_path, monkeypatch)
    _patch_build(monkeypatch, PINNED)

    a1 = Agent(enable_log=False)
    await a1.ensure_initialized()
    a1.session.save_message("user", "hi")
    sid = a1.session.session_id
    pinned = a1._conv.system_prompt

    # "代码更新"：新构建出来的提示词完全不同
    _patch_build(monkeypatch, FRESH)
    a2 = Agent(resume=sid)
    await a2.ensure_initialized()

    assert a2._conv.system_prompt == pinned
    assert FRESH not in a2._conv.system_prompt


@pytest.mark.asyncio
async def test_pinned_skips_append_replay(tmp_path, monkeypatch):
    """固化版本已含插件段：不得再 append 一次（会改写前缀）。"""
    _mk_env(tmp_path, monkeypatch)
    _patch_build(monkeypatch, PINNED)

    from fp_core.core.lifecycle import LifecycleHook

    a1 = Agent(enable_log=False)

    async def plugin(ctx: Any, **kwargs: Any) -> Any:
        ctx.data.setdefault("system_prompt_append", []).append("【插件段】只应出现一次")
        return ctx

    a1.lifecycle.register(LifecycleHook.ON_INIT, plugin, name="p1")
    await a1.ensure_initialized()
    assert a1._conv.system_prompt.count("【插件段】") == 1
    a1.session.save_message("user", "hi")
    sid = a1.session.session_id

    a2 = Agent(resume=sid)
    a2.lifecycle.register(LifecycleHook.ON_INIT, plugin, name="p1")
    await a2.ensure_initialized()
    assert a2._conv.system_prompt.count("【插件段】") == 1


@pytest.mark.asyncio
async def test_rebuild_default_keeps_pinned_refresh_rebuilds(tmp_path, monkeypatch):
    _mk_env(tmp_path, monkeypatch)
    _patch_build(monkeypatch, PINNED)

    agent = Agent(enable_log=False)
    await agent.ensure_initialized()
    agent.session.save_message("user", "hi")
    pinned = agent.session.get_system_prompt()
    assert pinned

    _patch_build(monkeypatch, FRESH)

    # 默认：复用固化版本（resume / switch 走这条）
    agent.state.rebuild_system_prompt()
    assert agent.state.conversation.system_prompt == pinned

    # 显式刷新：重建并覆盖固化。注意 refresh 会**重放**插件注入段（task_system/
    # zeta/memory 等真实插件在测试环境也会注册），所以结果 = 新基座 + 注入段，
    # 而不是只剩新基座。
    agent.state.rebuild_system_prompt(refresh=True)
    refreshed = agent.state.conversation.system_prompt
    assert refreshed.strip().startswith(FRESH)
    assert agent.session.get_system_prompt() == refreshed
    # 注入段不得因刷新而重复堆叠
    for seg in agent.state.prompt_appends:
        assert refreshed.count(seg) == 1


@pytest.mark.asyncio
async def test_clear_keeps_pinned_prompt(tmp_path, monkeypatch):
    _mk_env(tmp_path, monkeypatch)
    _patch_build(monkeypatch, PINNED)

    agent = Agent(enable_log=False)
    await agent.ensure_initialized()
    agent.session.save_message("user", "hi")
    pinned = agent.session.get_system_prompt()

    agent.session.clear_session_file()
    assert agent.session.get_system_prompt() == pinned
