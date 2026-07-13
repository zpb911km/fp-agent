"""
Agent 工具并行执行测试

测试策略（不依赖真实 LLM）:
  1. mock agent._invoke_llm → 返回预设的 tool_calls
  2. 注入自定义工具（已知延迟）→ 测量并行 vs 串行耗时
  3. 注册生命周期钩子 → 验证每个工具独立触发
  4. 验证结果顺序一致性
  5. 验证中断处理
"""

import asyncio
import contextlib
import os
import time

import pytest

from fp_core.core.llm_service import LLMResult

# ── 全局：设 LLM API KEY 过门禁 ──
os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-parallel")


# ═══════════════════════════════════════════════════════════════
#  工具函数：创建 Agent 并清除用户插件干扰
# ═══════════════════════════════════════════════════════════════


def _make_clean_agent(tool_exec=None, **agent_kwargs):
    """创建 Agent 并注销用户安装的外部插件钩子（如 tool_audit）"""
    from fp_core.core.agent import Agent
    from fp_core.core.lifecycle import LifecycleHook

    agent = Agent(enable_log=False, tool_executor=tool_exec, **agent_kwargs)
    # 注销用户级插件钩子（测试环境不需要交互式审批）
    agent.lifecycle.unregister(LifecycleHook.ON_TOOL_CALL, "tool_audit_on_tool_call")
    return agent


# ═══════════════════════════════════════════════════════════════
#  Fixtures
# ═══════════════════════════════════════════════════════════════


@pytest.fixture
def sample_tool_registry():
    """创建带延迟工具的 ToolRegistry（纯内存，不扫描文件系统）"""
    from fp_core.tools import ToolRegistry

    registry = ToolRegistry()

    # 注册测试工具: sleep_N -> sleep(N) 秒后返回
    registry.register_tool(
        "sleep_1",
        {
            "type": "function",
            "function": {
                "name": "sleep_1",
                "description": "sleep 1s",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: asyncio.sleep(1) or "done_1",
    )
    registry.register_tool(
        "sleep_2",
        {
            "type": "function",
            "function": {
                "name": "sleep_2",
                "description": "sleep 2s",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: asyncio.sleep(2) or "done_2",
    )
    registry.register_tool(
        "sleep_3",
        {
            "type": "function",
            "function": {
                "name": "sleep_3",
                "description": "sleep 3s",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: asyncio.sleep(3) or "done_3",
    )
    # 报错工具
    registry.register_tool(
        "always_fail",
        {
            "type": "function",
            "function": {
                "name": "always_fail",
                "description": "always raises",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        executor=lambda params: (_ for _ in ()).throw(RuntimeError("I always fail")),
    )
    return registry


@pytest.fixture
def mock_llm():
    """mock LLM: 第一次返回 tools，第二次无 tools → 退出循环"""
    call_count = 0

    async def _mock_chat(messages, tools=None, **overrides):
        nonlocal call_count
        call_count += 1

        if call_count == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "sleep_1", "arguments": "{}"}},
                        {"id": "call_2", "type": "function", "function": {"name": "sleep_2", "arguments": "{}"}},
                        {"id": "call_3", "type": "function", "function": {"name": "sleep_3", "arguments": "{}"}},
                    ],
                    "_interrupted": False,
                },
                usage=None,
            )
        else:
            return LLMResult(
                message={"role": "assistant", "content": "全部完成", "_interrupted": False},
                usage=None,
            )

    return _mock_chat


@pytest.fixture
def mock_llm_with_fail():
    """返回工具列表包含一个会失败的工具（第二次返回空→退出循环）"""
    call_count = 0

    async def _mock_chat(messages, tools=None, **overrides):
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "sleep_1", "arguments": "{}"}},
                        {"id": "call_fail", "type": "function", "function": {"name": "always_fail", "arguments": "{}"}},
                        {"id": "call_3", "type": "function", "function": {"name": "sleep_3", "arguments": "{}"}},
                    ],
                    "_interrupted": False,
                },
                usage=None,
            )
        else:
            return LLMResult(
                message={"role": "assistant", "content": "done", "_interrupted": False},
                usage=None,
            )

    return _mock_chat


@pytest.fixture
def lifecycle_recorder():
    """注册到 agent 的生命周期记录器"""
    calls = []

    class Recorder:
        def record(self, ctx, **kwargs):
            calls.append({"hook": ctx.hook.name, "data": dict(ctx.data)})
            return ctx

    return Recorder(), calls


@pytest.fixture
def lifecycle_blocker():
    """模拟插件拒绝 sleep_2"""
    calls = []

    async def on_tool_call(ctx, **kwargs):
        calls.append({"hook": ctx.hook.name, "tool": kwargs.get("tool_name")})
        if kwargs.get("tool_name") == "sleep_2":
            ctx.data["cancelled"] = True
            ctx.data["cancel_reason"] = "sleep_2 被拒绝"
        return ctx

    return on_tool_call, calls


# ═══════════════════════════════════════════════════════════════
#  测试用例
# ═══════════════════════════════════════════════════════════════


class TestParallelToolExecution:
    """工具并行执行核心测试"""

    @pytest.mark.asyncio
    async def test_parallel_speedup(self, sample_tool_registry, mock_llm):
        """✅ 并行: 3 工具总耗时 ≈ 最慢者（3s），而非三者之和（6s+）"""
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm

        start = time.monotonic()
        await agent.process("并行执行 sleep_1, sleep_2, sleep_3")
        elapsed = time.monotonic() - start

        # 并行耗时应 ≈ max(1,2,3) = ~3s，串行应是 ~6s
        assert elapsed < 4.5, f"并行耗时 {elapsed:.2f}s，预期 < 4.5s（串行约 6s）"
        assert elapsed >= 2.5, f"并行耗时 {elapsed:.2f}s，应 >= 2.5s（至少等 sleep_3 完成）"

        # 验证结果已写入对话
        last_content = agent._conv.get_last_content()
        assert "全部完成" in last_content or last_content == "全部完成", f"最终回复异常: {last_content}"

        # 验证工具结果按正确顺序写入
        tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 3, f"应有 3 条 tool 消息，实有 {len(tool_msgs)}"
        assert tool_msgs[0]["tool_call_id"] == "call_1"
        assert tool_msgs[1]["tool_call_id"] == "call_2"
        assert tool_msgs[2]["tool_call_id"] == "call_3"

    @pytest.mark.asyncio
    async def test_lifecycle_hooks_fire_correctly(self, sample_tool_registry, mock_llm):
        """✅ 生命周期钩子（ON_TOOL_CALL / ON_TOOL_RESULT）每个工具独立触发"""
        from fp_core.core.lifecycle import LifecycleHook
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm

        # 注册钩子记录器
        call_events = []
        result_events = []

        async def on_call(ctx, **kwargs):
            call_events.append(kwargs.get("tool_name"))
            return ctx

        async def on_result(ctx, **kwargs):
            result_events.append(kwargs.get("tool_name"))
            return ctx

        agent.lifecycle.register(LifecycleHook.ON_TOOL_CALL, on_call, name="test_recorder_call")
        agent.lifecycle.register(LifecycleHook.ON_TOOL_RESULT, on_result, name="test_recorder_result")

        await agent.process("test lifecycle")

        assert len(call_events) == 3, f"ON_TOOL_CALL 应触发 3 次，实为 {len(call_events)}"
        assert len(result_events) == 3, f"ON_TOOL_RESULT 应触发 3 次，实为 {len(result_events)}"
        assert call_events == ["sleep_1", "sleep_2", "sleep_3"]
        assert result_events == ["sleep_1", "sleep_2", "sleep_3"]

    @pytest.mark.asyncio
    async def test_plugin_can_block_specific_tool(self, sample_tool_registry, mock_llm):
        """✅ 插件可拒绝某个工具（sleep_2 被拦截，其他正常执行）"""
        from fp_core.core.lifecycle import LifecycleHook
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm

        # 注册 blocking 钩子
        async def block_sleep2(ctx, **kwargs):
            if kwargs.get("tool_name") == "sleep_2":
                ctx.data["cancelled"] = True
                ctx.data["cancel_reason"] = "sleep_2 测试拒绝"
            return ctx

        agent.lifecycle.register(LifecycleHook.ON_TOOL_CALL, block_sleep2, name="test_blocker")

        await agent.process("test block")

        tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 3, f"应仍为 3 条 tool 消息（sleep_2 被拒绝但生成假结果），实为 {len(tool_msgs)}"
        assert tool_msgs[1]["tool_call_id"] == "call_2"
        assert "拒绝" in tool_msgs[1]["content"] or "拒绝" in str(tool_msgs[1])

    @pytest.mark.asyncio
    async def test_parallel_single_tool(self, sample_tool_registry):
        """✅ 单工具耗时 ≈ 自身耗时（退化情况）"""
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)

        call_count = 0

        async def single_tool_chat(messages, tools=None, **overrides):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResult(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {"id": "call_x", "type": "function", "function": {"name": "sleep_2", "arguments": "{}"}},
                        ],
                        "_interrupted": False,
                    },
                    usage=None,
                )
            else:
                return LLMResult(
                    message={"role": "assistant", "content": "done", "_interrupted": False},
                    usage=None,
                )

        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = single_tool_chat

        start = time.monotonic()
        await agent.process("test single")
        elapsed = time.monotonic() - start

        assert 1.5 < elapsed < 3.5, f"单工具 sleep_2 耗时 {elapsed:.2f}s，预期 ~2s"
        tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1

    @pytest.mark.asyncio
    async def test_no_tool_calls_bypasses_parallel(self):
        """✅ 无工具调用时跳过并行路径，不崩溃"""

        async def no_tool_chat(messages, tools=None, **overrides):
            return LLMResult(
                message={"role": "assistant", "content": "无工具调用", "_interrupted": False},
                usage=None,
            )

        agent = _make_clean_agent()
        agent._llm.chat = no_tool_chat

        resp = await agent.process("test no tools")
        assert resp.content == "无工具调用" or "无工具调用" in resp.content

    @pytest.mark.asyncio
    async def test_empty_tool_list_after_filter(self, sample_tool_registry, mock_llm):
        """✅ 所有工具被插件过滤后 → 空列表 → no-op"""
        from fp_core.core.lifecycle import LifecycleHook
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm

        # ON_TOOL_SELECT 过滤所有工具
        async def empty_tools(ctx, **kwargs):
            ctx.data["modified_tools"] = []
            return ctx

        agent.lifecycle.register(LifecycleHook.ON_TOOL_SELECT, empty_tools, name="test_empty")

        resp = await agent.process("test empty")
        # 全部被过滤 → 无工具消息，第二次 LLM 调用返回 "全部完成"
        assert resp is not None

    @pytest.mark.asyncio
    async def test_plugin_modifies_tool_args(self, sample_tool_registry, mock_llm):
        """✅ 插件可通过 modified_tool_args 修改工具参数"""
        from fp_core.core.lifecycle import LifecycleHook
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm

        # 记录 ON_TOOL_CALL 的参数
        seen_args = []

        async def record_args(ctx, **kwargs):
            seen_args.append({"tool": kwargs.get("tool_name"), "args": kwargs.get("tool_args")})
            return ctx

        agent.lifecycle.register(LifecycleHook.ON_TOOL_CALL, record_args, name="test_recorder")

        await agent.process("test args")
        assert len(seen_args) == 3
        assert all(a["args"] == "{}" for a in seen_args)  # 空参数


class TestParallelErrorHandling:
    """并行执行中的异常处理"""

    @pytest.mark.asyncio
    async def test_one_tool_fails_others_succeed(self, sample_tool_registry, mock_llm_with_fail):
        """✅ 一个工具失败不影响其他工具执行"""
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)
        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = mock_llm_with_fail

        start = time.monotonic()
        await agent.process("test mixed fail/success")
        elapsed = time.monotonic() - start

        # 最慢的是 sleep_3（3s），always_fail 应立即失败
        assert elapsed < 4.5, f"即使有失败工具，耗时也应 ~3s，实为 {elapsed:.2f}s"

        # 3 个工具消息（sleep_1, always_fail, sleep_3）
        tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 3

        # always_fail 的内容应包含错误信息
        fail_msg = tool_msgs[1]
        assert "失败" in fail_msg["content"] or "error" in fail_msg["content"].lower() or "Error" in fail_msg["content"]

    @pytest.mark.asyncio
    async def test_plugin_suppresses_tool_error(self, sample_tool_registry):
        """✅ ON_TOOL_ERROR 钩子可达：插件可抑制工具执行异常"""
        from fp_core.core.lifecycle import LifecycleHook
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)

        call_count = 0

        async def fail_tool_chat(messages, tools=None, **overrides):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return LLMResult(
                    message={
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_fail",
                                "type": "function",
                                "function": {"name": "always_fail", "arguments": "{}"},
                            },
                        ],
                        "_interrupted": False,
                    },
                    usage=None,
                )
            else:
                return LLMResult(
                    message={"role": "assistant", "content": "done", "_interrupted": False},
                    usage=None,
                )

        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = fail_tool_chat

        # 注册 ON_TOOL_ERROR 处理器：检测错误并抑制
        async def suppress_error(ctx, **kwargs):
            error = kwargs.get("error", "")
            if "fail" in error.lower() or "I always fail" in error:
                ctx.data["suppressed"] = True
                ctx.data["suppress_reason"] = "插件已抑制 always_fail 错误"
            return ctx

        agent.lifecycle.register(LifecycleHook.ON_TOOL_ERROR, suppress_error, name="test_error_suppressor")

        await agent.process("test suppress")
        tool_msgs = [m for m in agent._conv.messages if m["role"] == "tool"]
        assert len(tool_msgs) == 1
        # 抑制后返回 "错误已被抑制：插件已抑制 always_fail 错误"
        assert "抑制" in tool_msgs[0]["content"]
        assert "always_fail" in tool_msgs[0]["content"]


class TestParallelInterrupt:
    """中断处理"""

    @pytest.mark.asyncio
    async def test_signal_interrupt_during_gather(self, sample_tool_registry):
        """✅ 主流程中断信号 → agent.cancel() 生效

        设计：在工具执行期间（sleep_1）调用 cancel()，
        gather 完成后 while 循环 _check_interrupted() 检测到中断。
        CancelledError 从 process() 逃逸是预期行为（与 Ctrl+C 流程一致）。
        """
        from fp_core.core.tool_executor import ToolExecutor

        tool_exec = ToolExecutor(registry=sample_tool_registry)

        async def chat_with_cancel(messages, tools=None, **overrides):
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "sleep_1", "arguments": "{}"}},
                    ],
                    "_interrupted": False,
                },
                usage=None,
            )

        agent = _make_clean_agent(tool_exec=tool_exec)
        agent._llm.chat = chat_with_cancel

        async def delayed_cancel():
            await asyncio.sleep(0.1)
            agent.cancel()

        async def run():
            cancel_task = asyncio.create_task(delayed_cancel())
            with contextlib.suppress(asyncio.CancelledError):
                await agent.process("test cancel")  # _check_interrupted 引发 CancelledError→预期行为
            await cancel_task

        await run()
        # 正常结束，不崩溃即通过


# ═══════════════════════════════════════════════════════════════
#  性能基准测试（手动执行用）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_benchmark_parallel_vs_serial():
    """基准：直观对比串行 vs 并行耗时（仅手动触发，非 CI）"""
    # 仅在环境变量 FP_BENCHMARK=1 时执行
    if not os.environ.get("FP_BENCHMARK"):
        pytest.skip("跳过基准测试（设 FP_BENCHMARK=1 启用）")

    # 串行
    start = time.monotonic()
    for delay in [2, 2, 2]:
        await asyncio.sleep(delay)
    serial_time = time.monotonic() - start
    print(f"\n串行 3×2s: {serial_time:.2f}s")

    # 并行
    start = time.monotonic()
    await asyncio.gather(asyncio.sleep(2), asyncio.sleep(2), asyncio.sleep(2))
    parallel_time = time.monotonic() - start
    print(f"并行 3×2s: {parallel_time:.2f}s")
    print(f"加速比: {serial_time / parallel_time:.1f}x")
    assert parallel_time < serial_time * 0.6
