"""/reload 门卫回归 — 不得把自己那一轮算作「忙碌」。

背景（回归现场）：门卫曾判 ``is_run_active``，而 ``/reload`` 本身就是经
``process()`` 执行的**命令轮**——命令轮同样置位 ``_run_active``（空闲泵据此
让位），于是门卫把执行自己的那一轮也算成忙碌 → 空闲时也永远返回
「Agent 正在处理请求」，命令彻底不可用。

正确语义：门卫问的是「有没有**别的**轮（真正喂 LLM / 跑工具的轮）在跑」，
即 ``is_chat_round_active``。命令轮只置 ``_run_active``，不进非命令轮计数。

覆盖：
  1. 空闲时 /reload 穿过门卫、抵达激活核心（不是被自己挡住）
  2. 真有别的轮在跑时 /reload 被拒
  3. 计数不变量：命令轮执行期间 ``is_run_active=True`` 且
     ``is_chat_round_active=False``；普通轮/唤醒轮执行期间两者皆 True；轮结束复位
"""

import os
from pathlib import Path
from typing import Any

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core import handoff
from fp_core.core.agent import Agent
from fp_core.core.llm_service import LLMResult

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-reload-guard")


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
            # 轮内观察点：LLM 调用期间必须是「非命令轮在途」
            calls.append({
                "messages": list(messages),
                "chat_round": agent.is_chat_round_active,
                "run_active": agent.is_run_active,
            })
            return LLMResult(message={"role": "assistant", "content": "ok"}, usage=None)

        agent._llm.chat = mock_chat
        return agent, calls

    return _make


@pytest.fixture
def stub_reload(monkeypatch: pytest.MonkeyPatch):
    """把 execve 激活核心换成可观测 stub，并记录调用瞬间的忙碌信号。"""
    seen: list[dict[str, Any]] = []

    async def _stub(state: Any, *, kind: str) -> str:
        agent = state.agent
        seen.append({
            "kind": kind,
            "run_active": agent.is_run_active if agent else None,
            "chat_round": agent.is_chat_round_active if agent else None,
        })
        return "STUB_RELOAD_OK"

    monkeypatch.setattr(handoff, "perform_exec_reload", _stub)
    return seen


# ── 门卫语义 ───────────────────────────────────────────────


@pytest.mark.asyncio
async def test_reload_passes_when_idle(make_agent, stub_reload):
    """空闲时 /reload 必须穿过门卫（回归：曾因判 is_run_active 而永远拒绝）。"""
    agent, _ = make_agent()
    resp = await agent.process("/reload")

    assert resp.content == "STUB_RELOAD_OK", f"应抵达激活核心，实际: {resp.content!r}"
    assert stub_reload == [{"kind": "command", "run_active": True, "chat_round": False}], (
        "命令轮执行瞬间：_run_active=True（挡泵）且非命令轮计数=0（门卫不看自己）"
    )
    assert agent.is_run_active is False
    assert agent.is_chat_round_active is False


@pytest.mark.asyncio
async def test_reload_rejected_when_chat_round_active(make_agent, stub_reload):
    """真有别的整轮（LLM/工具轮）在跑 → 门卫必须拒绝，且不碰激活核心。"""
    agent, _ = make_agent()
    agent._chat_rounds = 1  # 与 test_wake/test_submit 同策略：直接置位模拟进行中

    resp = await agent.process("/reload")

    assert "正在处理请求" in resp.content, f"应拒绝，实际: {resp.content!r}"
    assert stub_reload == [], "被拒时不得进入激活核心"
    agent._chat_rounds = 0


@pytest.mark.asyncio
async def test_run_active_alone_does_not_block_reload(make_agent, stub_reload):
    """只有命令轮自身置位的 _run_active 不构成忙碌（泵位 ≠ 门卫位）。"""
    agent, _ = make_agent()
    assert agent.is_run_active is False
    assert agent.is_chat_round_active is False
    resp = await agent.process("/reload")
    assert resp.content == "STUB_RELOAD_OK"


# ── 计数不变量 ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_chat_round_counted_and_reset(make_agent):
    """普通轮期间 is_chat_round_active=True，轮结束复位（空闲泵/门卫依据）。"""
    agent, calls = make_agent()

    resp = await agent.process("hello")

    assert resp.content == "ok"
    assert calls and calls[0]["chat_round"] is True, "LLM 调用期间必须计入非命令轮"
    assert calls[0]["run_active"] is True
    assert agent.is_chat_round_active is False, "轮结束必须复位"
    assert agent.is_run_active is False


@pytest.mark.asyncio
async def test_wakeup_round_counts_then_resets(make_agent):
    """唤醒轮属非命令轮：执行期间计入，结束复位。"""
    from fp_core.core import jobs

    jobs.pending_inject.clear()
    agent, calls = make_agent()
    jobs.inject_event("peer_ring", "【铃声】…", wake=True)

    try:
        resp = await agent.process_wakeup()
        assert calls and calls[0]["chat_round"] is True
        assert resp.content == "ok"
        assert agent.is_chat_round_active is False
        assert agent.is_run_active is False
    finally:
        jobs.pending_inject.clear()


@pytest.mark.asyncio
async def test_command_round_does_not_count_as_chat_round(make_agent):
    """非 /reload 的普通命令同样不进非命令轮计数（命令轮统一语义）。"""
    agent, _ = make_agent()

    resp = await agent.process("/help")

    assert resp.content, "/help 应有输出"
    assert agent.is_chat_round_active is False
    assert agent.is_run_active is False
