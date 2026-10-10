"""WebUIBridge 单测 — 「一切用户消息经 ws 事件显示」的单一咽喉点语义。

覆盖：
  1. ON_CTX_APPEND(op="user") → user_message 广播
     （直发 portal.run.send 与环顶注入（排队消费/ask 回答/泵注入）同此钩子）
  2. op ∈ {assistant, tool, handoff} → 不发
  3. ON_COMMAND（handled 或有输出）→ user_message(ephemeral=True)，
     命令文本按 agent.process 的 split(maxsplit=1) 规则重组（name 已含斜杠）
  4. fallthrough（handled=False 且无输出）→ 不发（由 ON_CTX_APPEND 兜底防双渲染）
  5. 桥自身异常静默降级，不 stop_propagation 打断对话主链
"""

from __future__ import annotations

import asyncio

from fp_core.core.events import EventBus
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_webui.bridge import WebUIBridge


def _run(coro):
    return asyncio.run(coro)


def _drain(bus: EventBus) -> list[dict]:
    """取走环形缓冲里的全部事件"""
    return list(bus._buffer)


def test_ctx_append_user_publishes_user_message():
    """op='user' 的入库 → 广播 user_message（content 原样透传）"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="user",
            message={"role": "user", "content": "帮我查一下天气"},
            msg_count=3,
            messages=[],
        )

    _run(run())
    events = _drain(bus)
    assert len(events) == 1
    assert events[0]["type"] == "user_message"
    assert events[0]["content"] == "帮我查一下天气"
    assert "ephemeral" not in events[0]
    assert "seq" in events[0] and events[0]["seq"] == 1


def test_ctx_append_non_user_ops_silent():
    """assistant/tool/handoff 入库不产生 user_message"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        for op in ("assistant", "tool", "handoff"):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op=op,
                message={"content": "x"},
                msg_count=1,
                messages=[],
            )

    _run(run())
    assert _drain(bus) == []


def test_ctx_append_empty_content_silent():
    """空内容（异常兜底）不广播，避免前端渲染空白气泡"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        await lm.emit(LifecycleHook.ON_CTX_APPEND, op="user", message={}, msg_count=1, messages=[])

    _run(run())
    assert _drain(bus) == []


def test_command_handled_echoes_ephemeral():
    """已执行命令：输入不落盘 → ephemeral 回显（name 含斜杠 + arg 重组）"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/back",
            arg="5 2",
            output="已回溯",
            handled=True,
            latency_ms=1.0,
            messages=[],
        )
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/sessions",
            arg="",
            output="会话列表",
            handled=True,
            latency_ms=1.0,
            messages=[],
        )

    _run(run())
    events = _drain(bus)
    assert len(events) == 2
    assert events[0] == {**events[0], "type": "user_message", "content": "/back 5 2", "ephemeral": True}
    assert events[1]["content"] == "/sessions"
    assert events[1]["ephemeral"] is True


def test_command_error_output_still_echoes():
    """命令执行失败（handled=False 但有报错输出）：仍需回显输入"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/bogus",
            arg="",
            output="未知命令",
            handled=False,
            latency_ms=0.5,
            messages=[],
        )

    _run(run())
    events = _drain(bus)
    assert len(events) == 1
    assert events[0]["content"] == "/bogus"
    assert events[0]["ephemeral"] is True


def test_command_fallthrough_emits_nothing():
    """显式让渡（handled=False 且无输出）：此处不发，稍后真正 append 走
    ON_CTX_APPEND → 单一事件，防双渲染"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/let",
            arg="me through",
            output="",
            handled=False,
            latency_ms=0.5,
            messages=[],
        )

    _run(run())
    assert _drain(bus) == []


def test_publish_failure_does_not_break_emit():
    """桥发布失败必须静默：emit 不抛、不 stop_propagation（对话主链无感）"""

    class _BoomBus:
        async def publish(self, event):
            raise RuntimeError("bus down")

    bridge = WebUIBridge(_BoomBus())
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        # 正常注册后续观察者：桥异常后仍应被执行（无 stop_propagation）
        seen = []
        lm.register(
            LifecycleHook.ON_CTX_APPEND,
            lambda ctx, **kw: seen.append(("append", kw.get("op"))),
            priority=50,
        )
        lm.register(
            LifecycleHook.ON_COMMAND,
            lambda ctx, **kw: seen.append(("command", kw.get("name"))),
            priority=50,
        )
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="user",
            message={"content": "x"},
            msg_count=1,
            messages=[],
        )
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/ok",
            arg="",
            output="y",
            handled=True,
            latency_ms=0.1,
            messages=[],
        )
        return seen

    assert _run(run()) == [("append", "user"), ("command", "/ok")]


def test_real_eventbus_end_to_end():
    """经真实 EventBus：订阅者能收到 user_message（seq/ts 齐全，可重放）"""
    bus = EventBus()
    bridge = WebUIBridge(bus)
    lm = LifecycleManager()
    bridge.on_register(lm)

    async def run():
        sub_id, queue = bus.subscribe()
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="user",
            message={"content": "queued 消费 / ask 回答 / 泵注入同此路径"},
            msg_count=2,
            messages=[],
        )
        got = await asyncio.wait_for(queue.get(), timeout=1)
        bus.unsubscribe(sub_id)
        return got

    got = _run(run())
    assert got["type"] == "user_message"
    assert got["seq"] == 1
    assert isinstance(got["ts"], float)
