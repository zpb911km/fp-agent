# ruff: noqa: F811 — make_agent 为跨文件导入的 pytest fixture，参数位引用非重定义
"""块3 测试 — LLM 循环区埋点 #13–#29

覆盖：
  #13 ON_INTERRUPT      环顶中断抛出前
  #14 ON_CTX_REPAIR     repair_tool_ordering 静默 mutation
  #15 ON_EARLY_EXIT     插件取消本轮 LLM 调用
  #17 ON_AFTER_LLM_CALL usage/model/latency 字段补强
  #19 ON_CANCEL         LLM 调用中断直抛
  #20/#21/#22/#23       重试链 LLM_RETRY / RETRY_OK / FATAL
  #24 ON_STREAM_STRIP   流式中断剥离
  #25 ON_TOOL_BLOCKED   block_tool_execution 守卫
  #26 ON_LLM_PASS       正常通过守卫
  #27 ON_CTX_APPEND     assistant 入库后（journal 关键）
  #28 ON_STREAM_INTERRUPT / #29 ON_TURN_END
"""

import asyncio

import pytest

from fp_core.core.lifecycle import LifecycleHook
from tests.test_entry_hooks import make_agent  # noqa: F401 — pytest fixture 复用

# ═══════════════════════════════════════════════════════════════
#  #13 中断
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_interrupt_fires_before_raise(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_INTERRUPT)
    agent._interrupted = True  # 模拟 signal handler 已置位
    with pytest.raises(asyncio.CancelledError):
        await agent.process("hello")
    assert rec.names() == ["ON_INTERRUPT"]
    assert rec.of(LifecycleHook.ON_INTERRUPT)[0]["data"]["location"] == "loop_top"


# ═══════════════════════════════════════════════════════════════
#  #14 repair 观察
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_ctx_repair_only_when_repaired(make_agent, monkeypatch):
    agent, rec = make_agent(LifecycleHook.ON_CTX_REPAIR)

    # 正常路径：无修复 → 不触发
    await agent.process("hello")
    assert rec.names() == []

    # 修复发生 → 触发一次（每轮环顶一次）
    calls = {"n": 0}
    orig = agent._conv.repair_tool_ordering

    def fake_repair() -> int:
        calls["n"] += 1
        return orig() if calls["n"] > 1 else 2  # 首轮假装修了 2 条

    monkeypatch.setattr(agent._conv, "repair_tool_ordering", fake_repair)
    await agent.process("world")
    events = rec.of(LifecycleHook.ON_CTX_REPAIR)
    assert len(events) == 1
    assert events[0]["data"]["repaired"] == 2
    assert isinstance(events[0]["data"]["messages"], list)


# ═══════════════════════════════════════════════════════════════
#  #15 早退
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_early_exit_when_plugin_cancels_llm(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_EARLY_EXIT, LifecycleHook.ON_AFTER_LLM_CALL)

    def cancel_llm(ctx, **kwargs):
        ctx.data["cancelled"] = True
        ctx.data["cancel_reason"] = "nope"

    agent.lifecycle.register(LifecycleHook.ON_BEFORE_LLM_CALL, cancel_llm, name="cancel_llm")
    resp = await agent.process("hello")
    assert resp.content == "nope"
    assert rec.names() == ["ON_EARLY_EXIT"]
    assert rec.of(LifecycleHook.ON_EARLY_EXIT)[0]["data"]["stage"] == "before_llm_call"


# ═══════════════════════════════════════════════════════════════
#  #17 AFTER_LLM_CALL 字段
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_after_llm_call_carries_usage_model_latency(make_agent):
    from fp_core.core.llm_service import LLMResult

    agent, rec = make_agent(LifecycleHook.ON_AFTER_LLM_CALL)

    async def mock_chat(messages, tools=None, **kw):
        return LLMResult(
            message={"role": "assistant", "content": "ok", "_interrupted": False},
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    agent._llm.chat = mock_chat
    await agent.process("hello")
    data = rec.of(LifecycleHook.ON_AFTER_LLM_CALL)[0]["data"]
    assert data["usage"] == {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    assert data["model"] == agent.model
    assert data["latency_ms"] >= 0


# ═══════════════════════════════════════════════════════════════
#  #19 取消直抛
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_cancel_emits_when_llm_raises_cancelled(make_agent):
    """#19：外层 except 捕获后 emit ON_CANCEL 再 re-raise。

    语义注记：asyncio.CancelledError 的主路径已被 _invoke_llm 内层吞掉并转为
    _interrupted 消息（走 #24/#28 保留已生成内容），到达外层 except 的实际是
    KeyboardInterrupt（或外层 await 点的取消）。此处测 KeyboardInterrupt 分支。
    """
    agent, rec = make_agent(LifecycleHook.ON_CANCEL, LifecycleHook.ON_AFTER_LLM_CALL)

    async def mock_chat(messages, tools=None, **kw):
        raise KeyboardInterrupt

    agent._llm.chat = mock_chat
    with pytest.raises(KeyboardInterrupt):
        await agent.process("hello")
    assert rec.names() == ["ON_CANCEL"]
    data = rec.of(LifecycleHook.ON_CANCEL)[0]["data"]
    assert data["stage"] == "llm_call"
    assert data["latency_ms"] >= 0


# ═══════════════════════════════════════════════════════════════
#  #20/#21 重试成功链
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_retry_success_chain(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_ERROR,
        LifecycleHook.ON_LLM_RETRY,
        LifecycleHook.ON_LLM_RETRY_OK,
        LifecycleHook.ON_AFTER_LLM_CALL,
        LifecycleHook.ON_CTX_APPEND,
    )
    calls = {"n": 0}

    async def mock_chat(messages, tools=None, **kw):
        calls["n"] += 1
        if calls["n"] == 1:
            raise ValueError("'tool' message must have preceding assistant")
        return __import__("fp_core.core.llm_service", fromlist=["LLMResult"]).LLMResult(
            message={"role": "assistant", "content": "recovered", "_interrupted": False},
            usage=None,
        )

    agent._llm.chat = mock_chat
    resp = await agent.process("hello")
    assert resp.content == "recovered"
    # 顺序：入口 user-append → ERROR → RETRY → RETRY_OK → AFTER → assistant-append
    assert rec.names() == [
        "ON_CTX_APPEND",  # op=user（入口）
        "ON_ERROR",
        "ON_LLM_RETRY",
        "ON_LLM_RETRY_OK",
        "ON_AFTER_LLM_CALL",
        "ON_CTX_APPEND",  # op=assistant
    ]
    assert rec.of(LifecycleHook.ON_LLM_RETRY)[0]["data"]["stage"] == "repair_then_retry"
    assert rec.of(LifecycleHook.ON_LLM_RETRY_OK)[0]["data"]["latency_ms"] >= 0


# ═══════════════════════════════════════════════════════════════
#  #22 重试仍失败 / #23 不可恢复
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_llm_fatal_retry_failed(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_LLM_FATAL, LifecycleHook.ON_CONTEXT_UPDATE)

    async def mock_chat(messages, tools=None, **kw):
        raise ValueError("'tool' message must have preceding assistant")

    agent._llm.chat = mock_chat
    await agent.process("hello")  # break → 正常收尾
    fatals = rec.of(LifecycleHook.ON_LLM_FATAL)
    assert len(fatals) == 1
    assert fatals[0]["data"]["stage"] == "retry_failed"


@pytest.mark.asyncio
async def test_llm_fatal_unrecoverable(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_LLM_FATAL, LifecycleHook.ON_LLM_RETRY)

    async def mock_chat(messages, tools=None, **kw):
        raise RuntimeError("boom")

    agent._llm.chat = mock_chat
    await agent.process("hello")
    fatals = rec.of(LifecycleHook.ON_LLM_FATAL)
    assert len(fatals) == 1
    assert fatals[0]["data"]["stage"] == "unrecoverable"
    assert "boom" in fatals[0]["data"]["error"]
    assert rec.names().count("ON_LLM_RETRY") == 0  # 非 tool 错误不进重试


# ═══════════════════════════════════════════════════════════════
#  #24/#26/#27/#28 流式中断剥离 + assistant 入库
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_stream_strip_and_interrupt(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_STREAM_STRIP,
        LifecycleHook.ON_LLM_PASS,
        LifecycleHook.ON_STREAM_INTERRUPT,
        LifecycleHook.ON_CTX_APPEND,
        LifecycleHook.ON_TOOL_SELECT,
    )

    # 注入 _invoke_llm（而非 _llm.chat）：_invoke_llm 正常路径会把 _interrupted
    # 无条件覆写为 False（agent.py:444），interrupted+tool_calls 只能在
    # CancelledError/断流分支产生。此处直接在该分支之后的产物上模拟。
    async def mock_invoke(context, silent=False):
        return (
            {
                "role": "assistant",
                "content": "partial",
                "_interrupted": True,
                "tool_calls": [
                    {"id": "t1", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
            None,
        )

    agent._invoke_llm = mock_invoke
    await agent.process("hello")
    names = rec.names()
    # 顺序：AFTER(未录) → STRIP → APPEND(assistant) → STREAM_INTERRUPT
    assert "ON_STREAM_STRIP" in names
    assert "ON_CTX_APPEND" in names
    assert "ON_STREAM_INTERRUPT" in names
    # 互斥：剥离时不发 LLM_PASS，且不进工具选择
    assert "ON_LLM_PASS" not in names
    assert "ON_TOOL_SELECT" not in names

    strip = rec.of(LifecycleHook.ON_STREAM_STRIP)[0]["data"]
    assert strip["tool_names"] == ["bash"]

    ca = rec.of(LifecycleHook.ON_CTX_APPEND)[-1]["data"]  # [-1]=assistant（[0] 是入口 user）
    assert ca["op"] == "assistant"
    assert "tool_calls" not in ca["message"]  # 已剥离
    assert "用户中断" in ca["message"]["content"]

    # 顺序断言：STRIP → assistant-APPEND → INTERRUPT（注意 names 里 CTX_APPEND 出现两次，取最后一次）
    _ia = len(names) - 1 - names[::-1].index("ON_CTX_APPEND")
    assert names.index("ON_STREAM_STRIP") < _ia < names.index("ON_STREAM_INTERRUPT")


# ═══════════════════════════════════════════════════════════════
#  #25 工具执行被守卫阻断
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tool_blocked_guard(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_TOOL_BLOCKED,
        LifecycleHook.ON_LLM_PASS,
        LifecycleHook.ON_TOOL_SELECT,
        LifecycleHook.ON_CTX_APPEND,
        LifecycleHook.ON_TURN_END,
    )

    def block_tools(ctx, **kwargs):
        ctx.data["block_tool_execution"] = True

    agent.lifecycle.register(LifecycleHook.ON_AFTER_LLM_CALL, block_tools, name="block_tools")

    async def mock_chat(messages, tools=None, **kw):
        from fp_core.core.llm_service import LLMResult

        return LLMResult(
            message={
                "role": "assistant",
                "content": "",
                "_interrupted": False,
                "tool_calls": [
                    {"id": "t1", "function": {"name": "bash", "arguments": "{}"}},
                ],
            },
            usage=None,
        )

    agent._llm.chat = mock_chat
    resp = await agent.process("hello")
    names = rec.names()
    assert "ON_TOOL_BLOCKED" in names
    assert "ON_LLM_PASS" not in names  # 互斥
    assert "ON_TOOL_SELECT" not in names  # tool_calls 已弹出 → 不进选择门
    # 弹出 tool_calls 后等价于正常终答 → TURN_END 收尾
    assert "ON_TURN_END" in names
    assert "ON_CTX_APPEND" in names
    ca = rec.of(LifecycleHook.ON_CTX_APPEND)[-1]["data"]  # [-1]=assistant（[0] 是入口 user）
    assert ca["op"] == "assistant"
    assert "tool_calls" not in ca["message"]
    assert resp.content == ""


# ═══════════════════════════════════════════════════════════════
#  #26/#27/#29 正常终答全链
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_normal_turn_full_chain(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_AFTER_LLM_CALL,
        LifecycleHook.ON_LLM_PASS,
        LifecycleHook.ON_CTX_APPEND,
        LifecycleHook.ON_TURN_END,
        LifecycleHook.ON_CONTEXT_UPDATE,
        LifecycleHook.ON_BEFORE_RESPONSE,
        LifecycleHook.ON_STREAM_STRIP,
        LifecycleHook.ON_TOOL_BLOCKED,
    )
    resp = await agent.process("hello")
    assert resp.content == "ok"
    names = rec.names()
    # 首条 CTX_APPEND 是入口 user 入库（块2 #11），其后才是循环区序列
    assert names == [
        "ON_CTX_APPEND",  # op=user（入口）
        "ON_AFTER_LLM_CALL",
        "ON_LLM_PASS",
        "ON_CTX_APPEND",  # op=assistant（#27）
        "ON_TURN_END",
        "ON_CONTEXT_UPDATE",
        "ON_BEFORE_RESPONSE",
    ]
    assert [e["data"]["op"] for e in rec.of(LifecycleHook.ON_CTX_APPEND)] == ["user", "assistant"]
    # #27 payload 完整性（journal 消费契约）
    ca = rec.of(LifecycleHook.ON_CTX_APPEND)[-1]["data"]  # [-1]=assistant（[0] 是入口 user）
    assert ca["op"] == "assistant"
    assert ca["message"]["role"] == "assistant"
    assert ca["msg_count"] >= 2
    assert isinstance(ca["messages"], list)
    # #26 payload
    assert rec.of(LifecycleHook.ON_LLM_PASS)[0]["data"]["response"]["content"] == "ok"
    # #29 payload
    assert rec.of(LifecycleHook.ON_TURN_END)[0]["data"]["content"] == "ok"


# ═══════════════════════════════════════════════════════════════
#  #17 modified_response 仍算通过守卫（#26 不因修改而消失）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_modified_response_still_passes(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_LLM_PASS, LifecycleHook.ON_CTX_APPEND)

    def modify(ctx, **kwargs):
        ctx.data["modified_response"] = {"role": "assistant", "content": "rewritten"}

    agent.lifecycle.register(LifecycleHook.ON_AFTER_LLM_CALL, modify, name="modify")
    resp = await agent.process("hello")
    assert resp.content == "rewritten"
    assert rec.of(LifecycleHook.ON_LLM_PASS)  # 通过守卫 → 仍发
    ca = rec.of(LifecycleHook.ON_CTX_APPEND)[-1]["data"]  # [-1]=assistant（[0] 是入口 user）
    assert ca["message"]["content"] == "rewritten"
