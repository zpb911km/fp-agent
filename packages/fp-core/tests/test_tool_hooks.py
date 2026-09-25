# ruff: noqa: F811 — 跨文件导入的 pytest fixture，参数位引用非重定义
"""块4 测试 — 工具子机埋点 #30–#44 + 两个顺带修复

覆盖：
  #31 ON_TOOL_CANCELLED     SELECT 拦截生效 + 缺陷②修复（补写占位，消除悬挂 tool_calls）
  #33 ON_TOOL_REJECTED      ON_TOOL_CALL 拒绝生效（仍过 RESULT 门）
  #34 ON_TOOL_EXEC          参数定稿后、真正执行前
  #35/#39 ON_TOOL_RESULT + ON_RESULT_MUTATED   RESULT 门三路统一（拒绝/抑制/成功）
  #37 ON_TOOL_SUPPRESSED    错误被抑制 — 缺陷①修复（抑制结果同样过 RESULT 门）
  #38 ON_TOOL_ERROR_PROPAGATED  未抑制错误经 gather 传播
  #40 ON_TOOL_RESULTS_READY gather 返回、按序消费前
  #41 ON_TOOL_ABORT         gather 整体被取消，全量补记
  #42 ON_CTX_APPEND(op=tool) 每条 tool 消息入库后（journal 的 ctx-after 行）
  #43 ON_TOOL_PARTIAL       遇中断元素：记 1 + 补记剩余
  #44 ON_ITERATION          工具轮完成回环顶
"""

import asyncio
import contextlib
import os
from typing import Any

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.lifecycle import LifecycleHook
from fp_core.core.llm_service import LLMResult
from fp_core.core.tool_executor import ToolExecutor
from fp_core.tools import ToolRegistry
from tests.test_agent_tool_parallel import _make_clean_agent  # noqa: F401
from tests.test_entry_hooks import Recorder

# ── LLM API KEY 过门禁 ──
os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-tool-hooks")


# ═══════════════════════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════════════════════


def make_agent(tmp_path, monkeypatch, registry: ToolRegistry, *attach: LifecycleHook):
    """隔离 Agent（session 落 tmp）+ 指定工具注册表 + 记录器"""

    def _make(*hooks: LifecycleHook) -> tuple[Any, Recorder]:
        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
        monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))

        agent = _make_clean_agent(tool_exec=ToolExecutor(registry=registry))
        rec = Recorder()
        rec.attach(agent.lifecycle, *(hooks or attach))
        return agent, rec

    return _make


def single_call_llm(tool_call_dicts: list[dict[str, Any]], final_content: str = "完成"):
    """第一次返回 tool_calls，之后返回纯文本退出循环"""
    call_count = 0

    async def _chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResult(
                message={"role": "assistant", "content": "", "tool_calls": tool_call_dicts, "_interrupted": False},
                usage=None,
            )
        return LLMResult(
            message={"role": "assistant", "content": final_content, "_interrupted": False},
            usage=None,
        )

    return _chat


def fast_registry() -> ToolRegistry:
    """三个秒回工具 + 一个必失败工具 + 一个取消工具 + 一个慢工具"""
    reg = ToolRegistry()

    def _add(name: str, executor: Any) -> None:
        reg.register_tool(
            name,
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": name,
                    "parameters": {"type": "object", "properties": {}, "required": []},
                },
            },
            executor=executor,
        )

    async def _ok(value: str) -> str:
        return value

    _add("t1", lambda params: _ok("ok_1"))
    _add("t2", lambda params: _ok("ok_2"))
    _add("t3", lambda params: _ok("ok_3"))
    _add("boom_cancel", lambda params: (_ for _ in ()).throw(asyncio.CancelledError()))
    _add("slow", lambda params: asyncio.sleep(5))

    return reg


def call(tool_id: str, name: str) -> dict[str, Any]:
    return {"id": tool_id, "type": "function", "function": {"name": name, "arguments": "{}"}}


# ═══════════════════════════════════════════════════════════════
#  #31 + 缺陷② SELECT 拦截 → 补写占位
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_select_cancelled_writes_placeholders(tmp_path, monkeypatch):
    """缺陷②修复：SELECT 拦截后必须为每个 tool_call 补写占位结果（消除悬挂）"""
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_CANCELLED, LifecycleHook.ON_CTX_APPEND)
    agent._llm.chat = single_call_llm([call("c1", "t1"), call("c2", "t2"), call("c3", "t3")])

    async def blocker(ctx: Any, **kwargs: Any) -> Any:
        ctx.data["cancelled"] = True
        ctx.data["cancel_reason"] = "全部拒绝"
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_TOOL_SELECT, blocker, name="test_blocker")

    await agent.process("go")

    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 3, f"应补写 3 条占位（原缺陷：悬挂 0 条），实为 {len(tool_msgs)}"
    assert all("被插件拦截" in m["content"] for m in tool_msgs)

    cancelled = rec.of(LifecycleHook.ON_TOOL_CANCELLED)
    assert len(cancelled) == 1
    assert cancelled[0]["data"]["placeholder_written"] is True
    assert cancelled[0]["data"]["tools"] == ["t1", "t2", "t3"]

    # 缺陷②下悬挂（assistant 有 tool_calls 而 tool 消息缺失）不允许出现
    for m in agent._conv.messages:
        if m.get("tool_calls"):
            ids = {tc["id"] for tc in m["tool_calls"]}
            answered = {t["tool_call_id"] for t in tool_msgs}
            assert ids <= answered, f"悬挂 tool_calls: {ids - answered}"


# ═══════════════════════════════════════════════════════════════
#  #34 ON_TOOL_EXEC 参数定稿后
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_tool_exec_observes_args(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_EXEC)
    agent._llm.chat = single_call_llm([call("c1", "t1")])

    async def modifier(ctx: Any, **kwargs: Any) -> Any:
        if kwargs.get("tool_name") == "t1":
            ctx.data["modified_tool_args"] = '{"rewritten": true}'
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_TOOL_CALL, modifier, name="test_modifier")

    await agent.process("go")

    execs = rec.of(LifecycleHook.ON_TOOL_EXEC)
    assert len(execs) == 1
    assert execs[0]["data"]["tool_name"] == "t1"
    # ON_TOOL_EXEC 在参数改写之后触发 → 观察到的是定稿参数
    assert "rewritten" in execs[0]["data"]["tool_args"]


# ═══════════════════════════════════════════════════════════════
#  #33 拒绝路径仍过 RESULT 门
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_rejected_path_keeps_result_gate(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_REJECTED, LifecycleHook.ON_TOOL_RESULT)
    agent._llm.chat = single_call_llm([call("c1", "t1"), call("c2", "t2")])

    async def blocker(ctx: Any, **kwargs: Any) -> Any:
        if kwargs.get("tool_name") == "t2":
            ctx.data["cancelled"] = True
            ctx.data["cancel_reason"] = "不给过"
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_TOOL_CALL, blocker, name="test_rejector")

    await agent.process("go")

    rejected = rec.of(LifecycleHook.ON_TOOL_REJECTED)
    assert len(rejected) == 1
    assert rejected[0]["data"]["tool_name"] == "t2"
    assert rejected[0]["data"]["reason"] == "不给过"

    # 拒绝路径仍过 RESULT 门（行为一致性）
    result_names = [e["data"]["tool_name"] for e in rec.of(LifecycleHook.ON_TOOL_RESULT)]
    assert "t2" in result_names, "拒绝结果必须经 ON_TOOL_RESULT"

    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 2
    assert "拒绝" in tool_msgs[1]["content"]


# ═══════════════════════════════════════════════════════════════
#  #37 + 缺陷① 抑制路径过 RESULT 门
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_suppressed_error_passes_result_gate(tmp_path, monkeypatch):
    """缺陷①修复：抑制结果必须经 ON_TOOL_RESULT（blocked 消费应生效）"""
    reg = fast_registry()
    reg.register_tool(
        "always_fail",
        {
            "type": "function",
            "function": {
                "name": "always_fail",
                "description": "fail",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: (_ for _ in ()).throw(RuntimeError("I always fail")),
    )
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(
        LifecycleHook.ON_TOOL_SUPPRESSED,
        LifecycleHook.ON_TOOL_ERROR,
        LifecycleHook.ON_TOOL_RESULT,
        LifecycleHook.ON_RESULT_MUTATED,
    )
    agent._llm.chat = single_call_llm([call("c1", "always_fail")])

    async def suppress(ctx: Any, **kwargs: Any) -> Any:
        ctx.data["suppressed"] = True
        ctx.data["suppress_reason"] = "已抑制"
        return ctx

    async def block_result(ctx: Any, **kwargs: Any) -> Any:
        if kwargs.get("tool_name") == "always_fail":
            ctx.data["blocked"] = True
            ctx.data["block_reason"] = "结果被过滤(插件)"
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_TOOL_ERROR, suppress, name="test_suppressor")
    agent.lifecycle.register(LifecycleHook.ON_TOOL_RESULT, block_result, name="test_blocker")

    await agent.process("go")

    # #36 错误先通知，#37 抑制生效
    assert len(rec.of(LifecycleHook.ON_TOOL_ERROR)) == 1
    assert len(rec.of(LifecycleHook.ON_TOOL_SUPPRESSED)) == 1

    # 缺陷①断言：抑制结果经 RESULT 门被 blocked 改写（修复前内容为 "错误已被抑制…"）
    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "结果被过滤(插件)", "缺陷①未修复：抑制结果绕过了 RESULT 门"

    mutated = rec.of(LifecycleHook.ON_RESULT_MUTATED)
    assert len(mutated) == 1
    assert mutated[0]["data"]["blocked"] is True


# ═══════════════════════════════════════════════════════════════
#  #38 传播 + #42 CTX_APPEND(op=tool)
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_error_propagated_and_ctx_append(tmp_path, monkeypatch):
    reg = fast_registry()
    reg.register_tool(
        "always_fail",
        {
            "type": "function",
            "function": {
                "name": "always_fail",
                "description": "fail",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: (_ for _ in ()).throw(RuntimeError("I always fail")),
    )
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_ERROR_PROPAGATED, LifecycleHook.ON_CTX_APPEND)
    agent._llm.chat = single_call_llm([call("c1", "always_fail"), call("c2", "t1")])

    await agent.process("go")

    # #38 未抑制 → 传播（由 gather 收集）
    prop = rec.of(LifecycleHook.ON_TOOL_ERROR_PROPAGATED)
    assert len(prop) == 1
    assert prop[0]["data"]["tool_name"] == "always_fail"
    assert "I always fail" in prop[0]["data"]["error"]

    # #42 每条 tool 消息入库后触发 op=tool，且 ctx-after 含该消息
    tool_appends = [e for e in rec.of(LifecycleHook.ON_CTX_APPEND) if e["data"]["op"] == "tool"]
    assert len(tool_appends) == 2, f"应 2 条 tool append，实为 {len(tool_appends)}"
    for e in tool_appends:
        assert e["data"]["message"]["role"] == "tool"
        assert e["data"]["messages"][-1] is e["data"]["message"], "ctx-after 末尾应为刚入库的消息"
    assert "❌" in tool_appends[0]["data"]["message"]["content"]


# ═══════════════════════════════════════════════════════════════
#  #35/#39 成功路径 RESULT 门变异
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_success_result_gate_mutates(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_RESULT, LifecycleHook.ON_RESULT_MUTATED)
    agent._llm.chat = single_call_llm([call("c1", "t1")])

    async def modifier(ctx: Any, **kwargs: Any) -> Any:
        if kwargs.get("tool_name") == "t1":
            ctx.data["modified_result"] = "被改写的结果"
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_TOOL_RESULT, modifier, name="test_modifier")

    await agent.process("go")

    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["content"] == "被改写的结果"

    mutated = rec.of(LifecycleHook.ON_RESULT_MUTATED)
    assert len(mutated) == 1
    assert mutated[0]["data"]["has_modified"] is True
    assert mutated[0]["data"]["blocked"] is False


# ═══════════════════════════════════════════════════════════════
#  #40 + #44 正常一轮：RESULTS_READY → 消费 → ITERATION
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_results_ready_and_iteration(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_RESULTS_READY, LifecycleHook.ON_ITERATION, LifecycleHook.ON_CTX_APPEND)
    agent._llm.chat = single_call_llm([call("c1", "t1"), call("c2", "t2"), call("c3", "t3")])

    await agent.process("go")

    ready = rec.of(LifecycleHook.ON_TOOL_RESULTS_READY)
    assert len(ready) == 1
    assert ready[0]["data"]["count"] == 3

    iteration = rec.of(LifecycleHook.ON_ITERATION)
    assert len(iteration) == 1
    assert iteration[0]["data"]["tools_executed"] == 3

    tool_appends = [e for e in rec.of(LifecycleHook.ON_CTX_APPEND) if e["data"]["op"] == "tool"]
    assert len(tool_appends) == 3
    assert [e["data"]["message"]["content"] for e in tool_appends] == ["ok_1", "ok_2", "ok_3"]

    # ITERATION 在本轮全部 tool append 之后（回环顶语义；assistant append 属下一轮，不计）
    tool_append_idxs = [
        i
        for i, e in enumerate(rec.events)
        if e["hook"] == LifecycleHook.ON_CTX_APPEND.name and e["data"]["op"] == "tool"
    ]
    assert max(tool_append_idxs) < rec.names().index(LifecycleHook.ON_ITERATION.name)


# ═══════════════════════════════════════════════════════════════
#  #43 中断元素 → 记 1 + 补记剩余
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_partial_interruption_records_remaining(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_PARTIAL, LifecycleHook.ON_CTX_APPEND)
    agent._llm.chat = single_call_llm(
        [call("c1", "t1"), call("c2", "boom_cancel"), call("c3", "t3")],
    )

    await agent.process("go")

    partial = rec.of(LifecycleHook.ON_TOOL_PARTIAL)
    assert len(partial) == 1, "遇中断元素应触发 ON_TOOL_PARTIAL"
    assert partial[0]["data"]["recorded"] == 2
    assert partial[0]["data"]["remaining"] == 1

    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 3, f"1 条中断记录 + 1 条补记 + 1 条成功，实为 {len(tool_msgs)}"
    assert tool_msgs[0]["content"] == "ok_1"
    assert "中断" in tool_msgs[1]["content"]
    assert "未执行" in tool_msgs[2]["content"]


# ═══════════════════════════════════════════════════════════════
#  #41 gather 整体被取消 → 全量补记 + ABORT
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_abort_on_gather_cancel(tmp_path, monkeypatch):
    reg = fast_registry()
    mk = make_agent(tmp_path, monkeypatch, reg)
    agent, rec = mk(LifecycleHook.ON_TOOL_ABORT, LifecycleHook.ON_CTX_APPEND)
    agent._llm.chat = single_call_llm([call("c1", "slow")])

    task = asyncio.ensure_future(agent.process("go"))
    await asyncio.sleep(0.3)  # 等待进入 gather
    task.cancel()
    with contextlib.suppress(asyncio.CancelledError, KeyboardInterrupt):
        await task

    abort = rec.of(LifecycleHook.ON_TOOL_ABORT)
    assert len(abort) == 1, "gather 整体取消应触发 ON_TOOL_ABORT"
    assert abort[0]["data"]["count"] == 1

    tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
    assert len(tool_msgs) == 1, "全量补记应写入中断占位"
    assert "中断" in tool_msgs[0]["content"]
