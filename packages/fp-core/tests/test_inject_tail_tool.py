"""忙时自动消息尾随 tool 注入（双路径 drain）测试。

覆盖（任务 #238 n1）：
  1. ConversationState.append_tail_to_last_tool 原语：末条 tool 才并入 /
     返回共享引用 / 空文本与空 content 边界
  2. drain 分流：自动消息（⁂[kind] 标头）+ 末条 tool → 并入 content 尾部，
     不新增消息、不发 ON_CTX_APPEND(op=user)
  3. 人类消息恒走环顶 user（_drained 标记），即使末条是 tool
  4. 末条非 tool（闲时唤醒轮 / 终答后）→ 自动消息走环顶 user（现状协议）
  5. FIFO 保序：[auto, human] 交错并入/追加保持相对顺序
  6. LLM 请求里可见并入内容（与 tool 同批发给 LLM）
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core import jobs
from fp_core.core.agent import Agent
from fp_core.core.conversation import ConversationState
from fp_core.core.llm_service import LLMResult

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-tail-tool")


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


def _seed_trailing_tool(agent: Agent) -> ConversationState:
    """构造 assistant(tool_calls) + tool 的合法配对，末条为 tool。"""
    conv = agent.state.conversation
    conv.add_assistant_message({
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "tc-1",
                "type": "function",
                "function": {"name": "bash", "arguments": "{}"},
            }
        ],
    })
    conv.add_tool_message("tc-1", "tool-result")
    return conv


# ── 1. 原语 ───────────────────────────────────────────────


def test_append_tail_merges_into_trailing_tool():
    conv = ConversationState()
    conv.add_tool_message("tc-1", "result")
    out = conv.append_tail_to_last_tool("⁂[job_done] x")
    assert out is not None
    assert out is conv._messages[-1], "必须返回共享引用（调用方可观察）"
    assert conv._messages[-1]["content"] == "result\n\n⁂[job_done] x"
    assert len(conv._messages) == 1, "并入不得新增消息"


def test_append_tail_none_when_last_not_tool():
    conv = ConversationState()
    conv.add_user_message("hi")
    assert conv.append_tail_to_last_tool("x") is None
    empty = ConversationState()
    assert empty.append_tail_to_last_tool("x") is None, "空会话不并入"
    sys_only = ConversationState(system_prompt="p")
    assert sys_only.append_tail_to_last_tool("x") is None, "末条 system 不并入"


def test_append_tail_empty_text_and_empty_content():
    conv = ConversationState()
    conv.add_tool_message("tc-1", "result")
    assert conv.append_tail_to_last_tool("") is None, "空文本 no-op"
    conv2 = ConversationState()
    conv2.add_tool_message("tc-1", "")
    out = conv2.append_tail_to_last_tool("tail")
    assert out is not None
    assert out["content"] == "tail", "原 content 为空时不带前导分隔符"


# ── 2. 自动消息 + 末条 tool → 并入 ────────────────────────


@pytest.mark.asyncio
async def test_auto_event_merges_into_trailing_tool(make_agent):
    agent, calls = make_agent()
    conv = _seed_trailing_tool(agent)
    before = len(conv.messages)

    jobs.inject_event("job_done", "任务完成", wake=True)
    await agent.process_wakeup()

    msgs = conv.messages
    assert len(msgs) == before + 1, "只允许 wakeup 终答 assistant 新增，并入不得另增消息"
    tool_msgs = [m for m in msgs if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert "⁂[job_done]" in tool_msgs[0]["content"]
    assert "任务完成" in tool_msgs[0]["content"], "自动消息必须并入 tool content"
    assert not any(m.get("role") == "user" for m in msgs), "并入路径不得落 user 消息"

    # 与 tool 同批发给 LLM（wakeup 轮首次调用即含并入内容）
    assert calls, "wakeup 轮应触发 LLM 调用"
    llm_tools = [m for m in calls[-1] if m.get("role") == "tool"]
    assert llm_tools, "LLM 请求必须含该 tool 消息"
    assert "任务完成" in llm_tools[0]["content"]


# ── 3. 人类消息恒走环顶 user ──────────────────────────────


@pytest.mark.asyncio
async def test_human_message_always_ring_top_even_with_trailing_tool(make_agent):
    agent, _ = make_agent()
    conv = _seed_trailing_tool(agent)
    before = len(conv.messages)

    jobs.inject_event("user_input", "补充一句", wake=True, human=True)
    await agent.process_wakeup()

    msgs = conv.messages
    human_new = [m for m in msgs[before:] if m.get("role") == "user"]
    assert len(human_new) == 1, "人类消息必须新增 user 消息（即使末条是 tool）"
    assert human_new[0]["content"].startswith("补充一句"), "人类话语裸文本落盘（可被 task 尾随注解追加）"
    assert human_new[0].get("_drained") is True, "人类话语必须带 _drained 标记"
    tool = [m for m in msgs if m.get("role") == "tool"][0]
    assert tool["content"] == "tool-result", "人类消息不得并入 tool"


# ── 4. 末条非 tool → 自动消息走环顶（现状协议） ───────────


@pytest.mark.asyncio
async def test_auto_event_ring_top_when_no_trailing_tool(make_agent):
    agent, _ = make_agent()
    conv = agent.state.conversation

    jobs.inject_event("job_done", "任务完成", wake=True)
    await agent.process_wakeup()

    user_msgs = [m for m in conv.messages if m.get("role") == "user"]
    assert len(user_msgs) == 1, "无尾随 tool 时自动消息走环顶 user（现状）"
    assert user_msgs[0]["content"].startswith("⁂[job_done]")
    assert not user_msgs[0].get("_drained"), "系统事件不打 _drained"


# ── 5. FIFO 保序 ──────────────────────────────────────────


@pytest.mark.asyncio
async def test_fifo_order_auto_then_human(make_agent):
    agent, _ = make_agent()
    conv = _seed_trailing_tool(agent)

    jobs.inject_event("job_done", "任务完成", wake=True)
    jobs.inject_event("user_input", "人类补充", wake=True, human=True)
    await agent.process_wakeup()

    msgs = conv.messages
    tool = [m for m in msgs if m.get("role") == "tool"][0]
    assert "任务完成" in tool["content"], "auto 应并入 tool"
    idx_tool = next(i for i, m in enumerate(msgs) if m.get("role") == "tool")
    idx_human = next(i for i, m in enumerate(msgs) if str(m.get("content") or "").startswith("人类补充"))
    assert idx_tool < idx_human, "FIFO：auto（并入 tool）必须先于 human（user）可见"
    human = msgs[idx_human]
    assert human.get("role") == "user" and human.get("_drained") is True


# ── 6. 观察：并入不发 op=user；环顶发 ─────────────────────


@pytest.mark.asyncio
async def test_observation_user_op_only_on_ring_top_path(make_agent):
    from fp_core.core.lifecycle import LifecycleHook

    agent, _ = make_agent()
    _seed_trailing_tool(agent)
    seen: list[Any] = []

    async def spy(ctx: Any, **kwargs: Any) -> None:
        if ctx.hook == LifecycleHook.ON_CTX_APPEND:
            seen.append(ctx.data.get("op"))

    agent.lifecycle.register(LifecycleHook.ON_CTX_APPEND, spy, name="test.spy")

    # 第一轮：末条 = tool → 并入，不得发 op=user（否则 bridge 按 append 校验误判）
    jobs.inject_event("job_done", "第一次", wake=True)
    await agent.process_wakeup()
    assert "user" not in seen, "并入路径不得发 ON_CTX_APPEND(op=user)"

    # 第二轮：末条 = assistant（终答后）→ 环顶，发 op=user（现状协议保持）
    jobs.inject_event("job_done", "第二次", wake=True)
    await agent.process_wakeup()
    assert "user" in seen, "环顶路径必须发 ON_CTX_APPEND(op=user)"
