"""唤醒机制测试 — wake 注入 / 空闲泵 / portal.run.wake（core 侧）。

覆盖：
  1. jobs：inject_event(wake=True) → has_pending_wake；drain 取走并清空
  2. Agent.process_wakeup：不追加用户输入，直接进循环消费注入（铃声落对话）
  3. is_run_active：唤醒轮结束后复位（空闲泵判断依据）
  4. portal.run.wake：空闲+待唤醒 → 起一轮；忙碌/无待处理 → None（人类优先）
"""

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

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-wake")


@pytest.fixture(autouse=True)
def _clean_injects():
    jobs.pending_inject.clear()
    yield
    jobs.pending_inject.clear()


@pytest.fixture
def make_agent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """真实 Agent + mock LLM + 隔离 session 目录（同 test_lifecycle_gaps 策略）。"""

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


# ── jobs 层 ─────────────────────────────────────────────


def test_wake_flag_and_has_pending_wake():
    assert not jobs.has_pending_wake()
    jobs.inject_event("job_done", "【系统事实】x")
    assert not jobs.has_pending_wake(), "普通注入默认非唤醒"
    jobs.inject_event("peer_ring", "【铃声】…", wake=True)
    assert jobs.has_pending_wake()


def test_drain_returns_kind_content_and_clears_wake():
    jobs.inject_event("peer_ring", "ring", wake=True)
    out = jobs.drain_ready()
    assert out == [{"kind": "peer_ring", "content": "ring"}]
    assert not jobs.has_pending_wake()
    assert jobs.drain_ready() == []


# ── agent 层 ────────────────────────────────────────────


@pytest.mark.asyncio
async def test_process_wakeup_consumes_injection_without_user_input(make_agent):
    agent, calls = make_agent()
    jobs.inject_event("peer_ring", "【铃声】peer:B 在找你（主题=contract）", wake=True)

    resp = await agent.process_wakeup()
    assert resp is not None
    assert calls, "唤醒轮应触发一次 LLM 调用"
    contents = [m.get("content", "") for m in agent.state.conversation.messages]
    assert any("【铃声】" in c for c in contents), "注入的铃声应以 user 角色落入对话"
    assert not jobs.has_pending_wake(), "唤醒事件已被消费"
    assert agent.is_run_active is False, "轮次结束后必须复位（空闲泵依据）"


@pytest.mark.asyncio
async def test_process_wakeup_no_injection_still_runs_loop(make_agent):
    """无注入时唤醒轮也会进循环（但 drain 为空，不追加用户消息）——防伪用户输入。"""
    agent, calls = make_agent()
    before = len(agent.state.conversation.messages)
    await agent.process_wakeup()
    # 不追加任何 user 输入（continuation 语义）
    user_msgs = [m for m in agent.state.conversation.messages[before:] if m.get("role") == "user"]
    assert user_msgs == []
    assert agent.is_run_active is False


# ── portal 层 ───────────────────────────────────────────


@pytest.mark.asyncio
async def test_portal_run_wake_idle_pending(make_agent):
    """空闲 + 待唤醒 → 起一轮并消费；随后无待处理 → None。"""
    from fp_core.api.portal import Portal

    agent, calls = make_agent()
    p = Portal()
    p._agent = agent  # 直接注入已开启实例（绕过 open 的 LLM 初始化）
    jobs.inject_event("peer_ring", "【铃声】peer:B 在找你", wake=True)

    resp = await p.run.wake()
    assert resp is not None
    assert calls, "唤醒应起一轮"
    assert not jobs.has_pending_wake()
    # 已消费 → 再次调用为 None（幂等）
    assert await p.run.wake() is None
    p._agent = None  # 清理


@pytest.mark.asyncio
async def test_portal_run_wake_busy_is_noop(make_agent):
    """忙碌（run 进行中）→ None（不打断，人类优先）。"""
    from fp_core.api.portal import Portal

    agent, calls = make_agent()
    p = Portal()
    p._agent = agent
    jobs.inject_event("peer_ring", "【铃声】", wake=True)

    agent._run_active = True  # 模拟进行中的一轮
    assert await p.run.wake() is None
    assert jobs.has_pending_wake(), "忙碌时事件保留，等空闲泵再取"
    agent._run_active = False
    p._agent = None


@pytest.mark.asyncio
async def test_portal_run_wake_closed_is_none():
    from fp_core.api.portal import Portal

    p = Portal()
    jobs.inject_event("peer_ring", "【铃声】", wake=True)
    assert await p.run.wake() is None  # 未开启实例 → None


# ── 空闲泵（自动） ──────────────────────────────────────


@pytest.mark.asyncio
async def test_wake_pump_autonomously_consumes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """空闲泵：ctl.open 后自动运行——注入 wake 即被消费，无需显式调用 run.wake。

    这是「空闲实例被铃声叫醒」的端到端机制（区别于 test_portal_run_wake_* 的手动调用）。
    """
    from fp_core.api.portal import Portal

    sessions = tmp_path / "sessions"
    sessions.mkdir(exist_ok=True)
    monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))

    p = Portal()
    await p.ctl.open(enable_log=False)
    try:
        agent = p._require()
        calls: list[Any] = []

        async def mock_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
            calls.append(list(messages))
            return LLMResult(message={"role": "assistant", "content": "ok"}, usage=None)

        agent._llm.chat = mock_chat
        jobs.inject_event("peer_ring", "【铃声】peer:B 在找你", wake=True)

        await asyncio.sleep(0.6)  # > 泵间隔（0.25s）
        contents = [m.get("content", "") for m in agent.state.conversation.messages]
        assert any("【铃声】" in c for c in contents), "空闲泵应自动起一轮消费 wake 注入"
        assert not jobs.has_pending_wake()
        assert calls, "唤醒轮应触发 LLM 调用"
    finally:
        await p.ctl.close()

    assert p._wake_pump_task is None, "ctl.close 后泵必须停止"
