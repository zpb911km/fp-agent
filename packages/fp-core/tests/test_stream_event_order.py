"""收尾事件顺序契约 — llm_end 不得越过 chunk / stream_end

Bug 现象（用户侧）：任务完成后，同一条最终回复在 WebUI 里显示两次。

根因：两条投递路径的落地时机不同。
  · WebSocketIO._pub       → `asyncio.ensure_future(publish(...))`（**排队**，不等待）
  · EventBridge._emit      → `await publish(...)`（**同步落地**）
于是 _invoke_llm 的 finally 里 `io.stream_end()` 只是把末批 token 排进队列，
紧随其后的 ON_AFTER_LLM_CALL → llm_end 立刻落地。真机抓到的帧序：
    llm_start → thinking_content… → llm_end → done → thinking_content → chunk → stream_end
前端只能把迟到正文并进"当前轮"的卡片；若跨轮，上一轮答案就落进新卡片 → 显示两次。

修法：生产者在交棒前 `await io.drain()`，把顺序钉成 token → stream_end → llm_end → done。
"""

import asyncio
import os

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod

# Agent 构造时 check_llm_config 要过门禁（与 test_entry_hooks 同模式）
os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-stream-order")


class _RecordingBus:
    """记录事件顺序的假总线（publish 同步落地，等价于真总线入队）"""

    def __init__(self) -> None:
        self.events: list[dict] = []

    async def publish(self, event: dict) -> None:
        self.events.append(event)

    def types(self) -> list[str]:
        return [e["type"] for e in self.events]


class _SlowBus(_RecordingBus):
    """每次投递都要跨一次 await 才落地：模拟真实网络/序列化耗时，
    让「没有 drain」与「有 drain」有确定性的区别（不靠调度巧合）。"""

    async def publish(self, event: dict) -> None:
        await asyncio.sleep(0.01)
        await super().publish(event)


@pytest.mark.asyncio
async def test_without_drain_close_event_overtakes_tokens():
    """回归锚点：不 drain 时 llm_end 越过排队中的 token（修好前的真机帧序）"""
    from fp_core.core.io import WebSocketIO

    bus = _RecordingBus()
    io = WebSocketIO(bus)
    io.stream_write("答")
    io.stream_end()
    await bus.publish({"type": "llm_end"})  # EventBridge 的 await publish：同步落地
    await asyncio.sleep(0.01)  # 让 _pub 排的 ensure_future 任务跑起来

    assert bus.types() == ["llm_end", "chunk", "stream_end"], (
        f"这个顺序正是 bug 的形状（收尾事件抢先），若不再复现说明 _pub 语义变了：{bus.types()}"
    )


@pytest.mark.asyncio
async def test_drain_puts_tokens_ahead_of_close_event():
    """修法：drain 之后顺序为 chunk → stream_end → llm_end"""
    from fp_core.core.io import WebSocketIO

    bus = _RecordingBus()
    io = WebSocketIO(bus)
    io.stream_write("答")
    io.stream_end()
    await io.drain()
    await bus.publish({"type": "llm_end"})

    assert bus.types() == ["chunk", "stream_end", "llm_end"]


@pytest.mark.asyncio
async def test_drain_waits_for_inflight_publish_completion():
    """drain 是「等落地」而不是「等排队」：慢投递也必须等完"""
    from fp_core.core.io import WebSocketIO

    bus = _SlowBus()
    io = WebSocketIO(bus)
    io.stream_write("答")
    io.stream_end()
    await io.drain()

    assert bus.types() == ["chunk", "stream_end"]  # 已真正落地
    assert not io._pending_pubs  # 在途集合清空


@pytest.mark.asyncio
async def test_drain_never_hangs_on_stuck_subscriber():
    """订阅者永久挂起时 drain 必须放弃等待（宁可漏等，不能卡住整轮对话）"""
    from fp_core.core.io import WebSocketIO

    class _StuckBus(_RecordingBus):
        async def publish(self, event: dict) -> None:
            await asyncio.Event().wait()  # 永不返回

    io = WebSocketIO(_StuckBus())
    io.stream_write("答")
    io.stream_end()
    await asyncio.wait_for(io.drain(), timeout=5.0)  # 不 raise 即通过（预算是 2s）


@pytest.mark.asyncio
async def test_agent_drains_before_after_llm_call(tmp_path, monkeypatch):
    """接线守护：真实 Agent 一轮里，llm_end 的发生点（ON_AFTER_LLM_CALL）
    必须已经在 stream_end 之后 —— 即 `await io.drain()` 还挂在 finally 里。"""
    from fp_core.core.agent import Agent
    from fp_core.core.io import WebSocketIO
    from fp_core.core.lifecycle import LifecycleHook
    from fp_core.core.llm_service import LLMResult

    sessions = tmp_path / "sessions"
    sessions.mkdir()
    monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
    monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))

    bus = _SlowBus()  # 慢投递：没 drain 就一定还没落地
    io = WebSocketIO(bus)
    agent = Agent(enable_log=False)

    async def mock_chat(messages: object, tools: object = None, **kw: object) -> LLMResult:
        return LLMResult(
            message={"role": "assistant", "content": "最终回复正文", "_interrupted": False},
            usage=None,
        )

    agent._llm.chat = mock_chat  # type: ignore[method-assign]

    landed: list[list[str]] = []

    async def spy(ctx: object, **kwargs: object) -> None:
        landed.append(bus.types())

    agent.lifecycle.register(LifecycleHook.ON_AFTER_LLM_CALL, spy, name="spy_drain_order")

    await agent.process("hi", io=io)

    assert landed, "ON_AFTER_LLM_CALL 未触发（测试自身失效）"
    assert "stream_end" in landed[0], f"llm_end 落地时流式输出还没发完 → 前端会把迟到正文并进下一轮：{landed[0]}"
