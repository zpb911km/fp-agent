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
        # msg_count 必须逐条递增：桥用连续性校验判定重写（断裂 → history_changed），
        # mock 里固定长度会被正确地识别为「凭空删了消息」
        for i, op in enumerate(("assistant", "tool", "handoff"), start=1):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op=op,
                message={"content": "x"},
                msg_count=i,
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
            # 长度与上方 append 后的 msg_count 对齐：命令未改上下文 → 不触发
            # history_changed，本测试只关心「桥异常不打断主链」
            messages=[{"role": "user", "content": "x"}],
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


# ═══════════════════════════════════════════════════════════════
# 连续性校验 — 上下文重写的通用检出
#   判据：所有入库都发 ON_CTX_APPEND(msg_count)，桥维护 expected 并跟随；
#   任何绕过 append 的整体重写（命令 set_messages、工具如 shortcircuit 退化）
#   都会让下一个观察点的 msg_count ≠ 期望 → 断裂 → history_changed。
# ═══════════════════════════════════════════════════════════════


def _bridge() -> tuple[EventBus, LifecycleManager]:
    bus = EventBus()
    lm = LifecycleManager()
    WebUIBridge(bus).on_register(lm)
    return bus, lm


def test_first_event_only_establishes_baseline():
    """首个观察点（启动/reload 后）只立基线，不得误报 history_changed"""
    bus, lm = _bridge()

    async def run():
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="tool",
            message={"content": "x"},
            msg_count=7,
            messages=[],
        )

    _run(run())
    assert _drain(bus) == []


def test_continuous_appends_silent():
    """连续单条入库（msg_count 严格 +1）+ 轮末观察点等长 → 全程无事件"""
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3, 4, 5):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        # 不改变长度的观察点：工具轮完成 / 终答 / 保存前
        await lm.emit(LifecycleHook.ON_ITERATION, tools_executed=2, msg_count=5)
        await lm.emit(LifecycleHook.ON_TURN_END, content="done", msg_count=5)
        await lm.emit(LifecycleHook.ON_CONTEXT_UPDATE, msg_count=5, messages=[])

    _run(run())
    assert _drain(bus) == []


def test_tool_rewrite_detected_by_append_jump():
    """工具（shortcircuit 退化历史块）重写上下文 → 下一条 append 的 msg_count 跳变

    这是「命令前后长度对比」完全覆盖不到的场景：整个改写过程不发生任何命令。
    """
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3, 4, 5):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        # 退化 5 → 2，随后工具返回入库 → 真实长度 3（按连续性应为 6）
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="tool",
            message={"content": "✅ 已退化"},
            msg_count=3,
            messages=[],
        )

    _run(run())
    events = _drain(bus)
    assert len(events) == 1
    assert events[0]["type"] == "history_changed"
    assert events[0]["msg_count"] == 3


def test_iteration_catches_rewrite_without_append():
    """工具改完上下文却**不 append** → 由 ON_ITERATION 的 msg_count 兜住"""
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        await lm.emit(LifecycleHook.ON_ITERATION, tools_executed=1, msg_count=2)

    _run(run())
    events = _drain(bus)
    assert len(events) == 1
    assert events[0]["type"] == "history_changed"
    assert events[0]["msg_count"] == 2


def test_turn_end_and_context_update_catch_tail_rewrite():
    """本轮末的重写（终答后 / 保存前）分别被 ON_TURN_END、ON_CONTEXT_UPDATE 兜住"""
    for hook, kwargs in (
        (LifecycleHook.ON_TURN_END, {"content": "done", "msg_count": 2}),
        (LifecycleHook.ON_CONTEXT_UPDATE, {"msg_count": 2, "messages": []}),
    ):
        bus, lm = _bridge()

        async def run(hook=hook, kwargs=kwargs, lm=lm):
            for c in (1, 2, 3, 4):
                await lm.emit(
                    LifecycleHook.ON_CTX_APPEND,
                    op="assistant",
                    message={"content": f"m{c}"},
                    msg_count=c,
                    messages=[],
                )
            await lm.emit(hook, **kwargs)

        _run(run())
        events = _drain(bus)
        assert [e["type"] for e in events] == ["history_changed"], hook
        assert events[0]["msg_count"] == 2


def test_command_rewrite_still_detected():
    """回归：改写型命令（/back /sc /clear /fork）仍被检出（统一判据）"""
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3, 4, 5):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/back",
            arg="2 2",
            output="已回溯",
            handled=True,
            latency_ms=1.0,
            messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        )

    _run(run())
    events = _drain(bus)
    assert [e["type"] for e in events] == ["history_changed", "user_message"]
    assert events[0]["msg_count"] == 2
    assert events[1]["ephemeral"] is True


def test_command_length_unchanged_silent():
    """回归：命令未改动上下文（长度与 expected 一致）→ 不发 history_changed"""
    bus, lm = _bridge()

    async def run():
        for c in (1, 2):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        await lm.emit(
            LifecycleHook.ON_COMMAND,
            name="/token",
            arg="",
            output="用量统计",
            handled=True,
            latency_ms=0.3,
            messages=[{"role": "user", "content": "a"}, {"role": "assistant", "content": "b"}],
        )

    _run(run())
    events = _drain(bus)
    assert [e["type"] for e in events] == ["user_message"]
    assert events[0]["content"] == "/token"


def test_break_does_not_cascade():
    """一次断裂只报一次：基线立即跟住真实值，后续正常 append 不再连环误报"""
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3, 4, 5):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        await lm.emit(  # 断裂：期望 6，实得 3
            LifecycleHook.ON_CTX_APPEND,
            op="tool",
            message={"content": "✅ 已退化"},
            msg_count=3,
            messages=[],
        )
        await lm.emit(  # 断裂后的正常入库：期望 4，实得 4 → 静默
            LifecycleHook.ON_CTX_APPEND,
            op="assistant",
            message={"content": "继续"},
            msg_count=4,
            messages=[],
        )

    _run(run())
    events = _drain(bus)
    assert [e["type"] for e in events] == ["history_changed"]


def test_repair_syncs_baseline_silently():
    """tool ordering 修复（长度可能变短）只同步基线、不发 history_changed

    repair 把孤立 tool 消息合并为合成 system 消息：属引擎内部一致性修复，
    前端卡片与落盘并无实质差异 → 不应 clearUI（会抹掉刚显示的中断提示）。
    """
    bus, lm = _bridge()

    async def run():
        for c in (1, 2, 3, 4):
            await lm.emit(
                LifecycleHook.ON_CTX_APPEND,
                op="assistant",
                message={"content": f"m{c}"},
                msg_count=c,
                messages=[],
            )
        # 修复：4 → 3（两条孤立 tool 合并成一条 system）
        await lm.emit(LifecycleHook.ON_CTX_REPAIR, repaired=1, messages=[{}, {}, {}])
        # 修复后的正常入库：期望 4，实得 4 → 静默
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="assistant",
            message={"content": "继续"},
            msg_count=4,
            messages=[],
        )

    _run(run())
    assert _drain(bus) == []
