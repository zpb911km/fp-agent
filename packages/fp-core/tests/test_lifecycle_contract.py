"""块1 契约测试：LifecycleHook 枚举扩至 50 + 新增 dataclass 字段补强

背景：agent loop 边集清单（54 边）中 34 个虚拟钩子 vON_* 正式化为枚举成员，
外加三方复审补出的 ON_COMMAND_BLOCKED（命令守卫分支的 post 出口），
总数 15 → 50。本文件锁死：
1. 枚举成员集合与计数（防漂移）
2. 新增 typed event context 的字段（F1 usage/model/latency_ms、F3 latency_ms、journal 关键 ctx-after 载荷）
3. 注册/emit 冒烟（新钩子走 LifecycleManager 全链路）
"""

from __future__ import annotations

import asyncio
from typing import Any

from fp_core.core.lifecycle import (
    AfterLLMCallEvent,
    CommandEvent,
    CtxAppendEvent,
    HookContext,
    LifecycleHook,
    LifecycleManager,
    SessionSaveEvent,
    ToolErrorEvent,
    ToolResultEvent,
)

# ── 权威清单：35 个新增钩子（原 15 之外）─────────────────────────
NEW_HOOKS = {
    # 输入/命令阶段
    "ON_INITIALIZED",
    "ON_INPUT",
    "ON_EMPTY",
    "ON_BEFORE_COMMAND",
    "ON_COMMAND",
    "ON_COMMAND_BLOCKED",
    "ON_FALLTHROUGH",
    "ON_MSG_ENTER",
    "ON_MESSAGE_BLOCKED",
    "ON_RESUME",
    # LLM循环阶段
    "ON_INTERRUPT",
    "ON_CTX_REPAIR",
    "ON_EARLY_EXIT",
    "ON_CANCEL",
    "ON_LLM_RETRY",
    "ON_LLM_RETRY_OK",
    "ON_LLM_FATAL",
    "ON_STREAM_STRIP",
    "ON_TOOL_BLOCKED",
    "ON_LLM_PASS",
    "ON_CTX_APPEND",
    "ON_STREAM_INTERRUPT",
    "ON_TURN_END",
    "ON_ITERATION",
    # 工具子机
    "ON_TOOL_CANCELLED",
    "ON_TOOL_REJECTED",
    "ON_TOOL_EXEC",
    "ON_TOOL_SUPPRESSED",
    "ON_TOOL_ERROR_PROPAGATED",
    "ON_RESULT_MUTATED",
    "ON_TOOL_RESULTS_READY",
    "ON_TOOL_ABORT",
    "ON_TOOL_PARTIAL",
    # 出口/存档
    "ON_SESSION_SAVE",
    "ON_CONTEXT_RESTORE",
}

OLD_HOOKS = {
    "ON_INIT",
    "ON_CONFIG_LOADED",
    "ON_MESSAGE_FILTER",
    "ON_MESSAGE_RECEIVED",
    "ON_BEFORE_LLM_CALL",
    "ON_AFTER_LLM_CALL",
    "ON_BEFORE_RESPONSE",
    "ON_TOOL_SELECT",
    "ON_TOOL_CALL",
    "ON_TOOL_RESULT",
    "ON_TOOL_ERROR",
    "ON_CONTEXT_UPDATE",
    "ON_ERROR",
    "ON_SHUTDOWN",
    "ON_CLEANUP",
}


def test_enum_membership_exact():
    """枚举成员 == 原15 + 新35 == 50，无重无漏"""
    names = {h.name for h in LifecycleHook}
    assert len(LifecycleHook) == 50, f"钩子总数应为 50，实际 {len(LifecycleHook)}"
    assert len(names) == len(LifecycleHook), "枚举成员重名"
    assert names == OLD_HOOKS | NEW_HOOKS, (
        f"缺: {(OLD_HOOKS | NEW_HOOKS) - names} 多: {names - (OLD_HOOKS | NEW_HOOKS)}"
    )
    assert not (OLD_HOOKS & NEW_HOOKS), "新旧集合交叉"


def test_new_hooks_register_and_emit():
    """全部 50 个钩子均可注册 + emit 冒烟（LifecycleManager 全链路）"""

    async def run() -> None:
        lm = LifecycleManager()
        fired: list[str] = []

        def make(h: LifecycleHook):
            def cb(ctx: HookContext, **kw: Any) -> None:
                fired.append(h.name)

            return cb

        for h in LifecycleHook:
            lm.register(h, make(h), name=f"t_{h.name}")
            await lm.emit(h, foo="bar")
        assert len(fired) == 50, f"仅 {len(fired)}/50 触发"

    asyncio.run(run())


def test_emit_kwargs_merge_into_data():
    """emit 的 kwargs 合并进 context.data（journal 插件依赖此机制取 ctx-after）"""

    async def run() -> None:
        lm = LifecycleManager()
        seen: dict[str, Any] = {}

        def grab(ctx: HookContext, **kw: Any) -> None:
            seen.update(ctx.data)

        lm.register(LifecycleHook.ON_CTX_APPEND, grab, name="grab")
        await lm.emit(
            LifecycleHook.ON_CTX_APPEND,
            op="assistant",
            message={"role": "assistant", "content": "hi"},
            msg_count=5,
            messages=[{"role": "user", "content": "u"}],
        )
        assert seen["op"] == "assistant"
        assert seen["msg_count"] == 5
        assert len(seen["messages"]) == 1

    asyncio.run(run())


# ── Typed event context 字段（F1/F3/journal）────────────────────


def test_after_llm_call_f1_fields():
    e = AfterLLMCallEvent(response={"role": "assistant"})
    assert e.usage is None
    assert e.model == ""
    assert e.latency_ms == 0.0
    # F1：逐调用统计可赋值
    e.usage = {"prompt_tokens": 10, "completion_tokens": 20}
    e.model = "qwen3-max"
    e.latency_ms = 1234.5


def test_command_event_payload():
    e = CommandEvent(name="/token", arg="", output="ctx: 1.2k", handled=True, latency_ms=3.2)
    assert e.output == "ctx: 1.2k"
    assert e.blocked is False  # ON_BEFORE_COMMAND 守卫位默认不触发


def test_ctx_append_payload():
    e = CtxAppendEvent(op="tool", message={"role": "tool", "content": "r"}, msg_count=8, messages=[])
    assert e.op in {"user", "assistant", "tool", "handoff"}
    assert e.msg_count == 8


def test_session_save_payload():
    e = SessionSaveEvent(session_id="sid", path="/tmp/x.jsonl", msg_count=3, messages=[])
    assert e.path == "/tmp/x.jsonl"


def test_tool_latency_f3_fields():
    r = ToolResultEvent(tool_name="bash", result="ok", latency_ms=42.0)
    assert r.latency_ms == 42.0
    err = ToolErrorEvent(tool_name="bash", error="boom", latency_ms=7.5)
    assert err.latency_ms == 7.5


def test_enum_count_matches_docs_authority():
    """与 test_docs_consistency 同源：数字声明的权威值锚点"""
    import re
    from pathlib import Path

    src = Path(__file__).parent.parent / "src" / "fp_core" / "core" / "lifecycle.py"
    m = re.search(r"class LifecycleHook\(Enum\):(.*?)(?=\n\n#|class )", src.read_text(encoding="utf-8"), re.S)
    assert m
    assert len(re.findall(r"^    ([A-Z_]+) = auto\(\)", m.group(1), re.M)) == 50
