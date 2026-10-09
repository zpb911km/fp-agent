"""portal.run.submit — 人类话语入队（数据面第二入口）测试。

覆盖（对应任务 #156 n4）：
  1. 返回值契约：queued / rejected_command / rejected_empty / not_open
  2. 身份协议：human=True 裸文本不打 ⁂[kind] 标头；wake=True 可被唤醒
  3. 空闲：submit 入队 → 主动 kick 唤醒（不等 0.25s 泵 tick）→ 消息以
     user 角色落盘 → ON_CTX_APPEND(op=user) 发出
  4. 忙碌：入队留置（人类优先，不打断），环顶 drain 同轮穿插消费
  5. 斜杠命令永不入队（运行中禁用的 core 侧兜底）
  6. io= 通道登记（与 send 同语义，供 reply() 转发 ask 应答）
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import Any

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core import jobs
from fp_core.core.agent import Agent
from fp_core.core.llm_service import LLMResult

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-submit")


@pytest.fixture(autouse=True)
def _clean_injects():
    jobs.pending_inject.clear()
    yield
    jobs.pending_inject.clear()


@pytest.fixture
def make_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 Agent + mock LLM + 隔离 session 目录（同 test_wake 策略）。"""

    def _make() -> tuple[Agent, list[Any]]:
        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
        monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))

        agent = Agent(enable_log=False)
        calls: list[Any] = []

        async def mock_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
            calls.append(list(messages))
            return LLMResult(message={"role": "assistant", "content": "ok"}, usage=None)

        agent._llm.chat = mock_chat
        return agent, calls

    return _make


def _open_portal(agent: Agent):
    from fp_core.api.portal import Portal

    p = Portal()
    p._agent = agent  # 直接注入已开启实例（绕过 open 的 LLM 初始化）
    return p


# ── 返回值契约 ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_not_open():
    from fp_core.api.portal import Portal

    assert await Portal().run.submit("hi") == "not_open"


@pytest.mark.asyncio
async def test_submit_rejects_empty_and_command(make_agent):
    agent, _ = make_agent()
    p = _open_portal(agent)
    assert await p.run.submit("   ") == "rejected_empty"
    assert await p.run.submit("/help") == "rejected_command"
    assert not jobs.pending_inject, "拒绝项不得入队"
    p._agent = None


@pytest.mark.asyncio
async def test_submit_command_not_queued_even_idle(make_agent):
    """斜杠命令永不入队——空闲也拒绝（空闲交互应走 run.send 完整命令链）。"""
    agent, _ = make_agent()
    p = _open_portal(agent)
    assert await p.run.submit("/resume foo") == "rejected_command"
    assert not jobs.pending_inject
    p._agent = None


# ── 身份协议 ────────────────────────────────────────────────


def test_submit_inject_is_human_bare_and_wake():
    """入队项：human=True 裸文本 + wake=True（可被 kick/泵立即消费）。"""
    from fp_core.core.jobs import drain_ready

    # 模拟 submit 的入队参数（core 层协议面）
    jobs.inject_event("user_input", "补充一句", wake=True, human=True)
    assert jobs.has_pending_wake()
    items = drain_ready()
    assert len(items) == 1
    assert items[0]["content"] == "补充一句", "人类话语必须裸文本，不打 ⁂[kind] 标头"
    assert not jobs.is_injected(items[0]["content"])


# ── 空闲：主动 kick 消费（不等泵 tick） ─────────────────────


@pytest.mark.asyncio
async def test_submit_idle_consumed_by_kick(make_agent):
    """空闲 submit → _kick_wake 立即起轮 → user 消息落盘（毫秒级，不等 0.25s）。"""
    agent, calls = make_agent()
    p = _open_portal(agent)
    try:
        assert await p.run.submit("请查一下天气") == "queued"
        # 主动 kick：远小于泵间隔（0.25s）即可完成消费
        assert p._wake_kick_task is not None
        await asyncio.wait_for(p._wake_kick_task, timeout=2.0)

        contents = [m.get("content", "") for m in agent.state.conversation.messages]
        assert "请查一下天气" in contents, "空闲 submit 应经 kick 立即消费落盘"
        assert not jobs.has_pending_wake()
        assert calls, "kick 起轮应触发 LLM 调用"
        # 落盘为 user 角色裸文本
        user_msgs = [
            m
            for m in agent.state.conversation.messages
            if m.get("role") == "user" and m.get("content") == "请查一下天气"
        ]
        assert user_msgs, "submit 消息必须以 user 角色落盘"
    finally:
        p._agent = None


@pytest.mark.asyncio
async def test_submit_idle_emits_ctx_append(make_agent):
    """环顶 drain 落盘必须发 ON_CTX_APPEND(op=user)（journal/插件观察点不丢）。"""
    agent, _ = make_agent()
    p = _open_portal(agent)
    seen: list[Any] = []

    from fp_core.core.lifecycle import LifecycleHook

    async def spy(ctx: Any, **kwargs: Any) -> None:
        if ctx.hook == LifecycleHook.ON_CTX_APPEND:
            seen.append(ctx.data.get("op"))

    from fp_core.core import lifecycle as lc_mod  # noqa: F401  (确保模块已加载)

    agent.lifecycle.register(LifecycleHook.ON_CTX_APPEND, spy, name="test.spy")
    try:
        assert await p.run.submit("观察点验证") == "queued"
        await asyncio.wait_for(p._wake_kick_task, timeout=2.0)
        assert "user" in seen, "submit 消费轮必须发 ON_CTX_APPEND(op=user)"
    finally:
        p._agent = None


# ── 忙碌：留置 + 环顶穿插 ───────────────────────────────────


@pytest.mark.asyncio
async def test_submit_busy_stays_queued_then_drains_at_ring_top(make_agent):
    """忙碌 submit → 留队不打断；下一轮环顶 drain 以 user 角色同轮穿插。"""
    agent, calls = make_agent()
    p = _open_portal(agent)
    try:
        # 伪造忙碌（与 test_wake 同策略：直接置位 run_active）
        agent._run_active = True
        assert await p.run.submit("运行中的补充") == "queued"
        assert jobs.has_pending_wake(), "忙碌时入队留置（人类优先，不打断）"
        # kick 尝试唤醒 → 忙碌应让位（wake 返回 None），消息仍在队
        await asyncio.wait_for(p._wake_kick_task, timeout=2.0)
        assert jobs.has_pending_wake(), "忙碌期 kick 必须让位，不得打断当前轮"

        # 释放忙碌 → 消费（环顶 drain 或空闲 kick 二路皆可）
        agent._run_active = False
        await p.run.wake()
        contents = [m.get("content", "") for m in agent.state.conversation.messages]
        assert "运行中的补充" in contents
        assert not jobs.has_pending_wake()
    finally:
        p._agent = None


@pytest.mark.asyncio
async def test_submit_multiple_burst_merges(make_agent):
    """连续多条 submit → 全量 drain 天然合并为一环（burst 语义同 wake 实测）。"""
    agent, calls = make_agent()
    p = _open_portal(agent)
    try:
        assert await p.run.submit("第一条") == "queued"
        assert await p.run.submit("第二条") == "queued"
        await asyncio.wait_for(p._wake_kick_task, timeout=2.0)

        contents = [m.get("content", "") for m in agent.state.conversation.messages]
        assert "第一条" in contents and "第二条" in contents
        assert len(calls) == 1, "burst 应合并为 1 轮 LLM 调用"
    finally:
        p._agent = None


# ── io 通道登记 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_submit_registers_active_io(make_agent):
    """submit(io=) 必须登记活跃通道，否则 submit 后 reply() 恒 False（同 send 语义）。"""
    from fp_core.core.io import IOChannel

    agent, _ = make_agent()
    p = _open_portal(agent)
    io = IOChannel()
    try:
        await p.run.submit("x", io=io)
        assert p._active_io is io, "submit 必须登记活跃 io 供 reply() 转发"
    finally:
        p._agent = None
        p._active_io = None
