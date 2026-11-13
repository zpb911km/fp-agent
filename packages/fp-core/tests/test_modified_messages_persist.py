# ruff: noqa: F811 — make_agent 为跨文件导入的 pytest fixture，参数位引用非重定义
"""modified_messages 落盘语义 — ON_BEFORE_LLM_CALL 写回 _conv

背景：`modified_messages` 原语义是「仅影响本次发给 LLM 的请求」，不写回会话。
task_system / ctx_trim 的尾随 hint 因此每轮丢失，下一轮原样重发 →
前缀在第一条被注解的消息处分叉 → 每轮固定损失数条消息的缓存命中。

改后契约（见 lifecycle.BeforeLLMCallEvent）：agent 收到 modified_messages
→ `_conv.replace_all()`（唯一事实源）→ 随轮末 `to_serializable()` 落盘。

本文件锁三件事：
  ① 改写既进内存 `_conv`，也进磁盘会话文件
  ② 同一条末条消息重复 fire 不叠第二份 hint（幂等 marker）
  ③ `_` 下划线协议标记（_drained 等）在设了 modified_messages 时也不出网
     —— 旧路径直接把 modified 列表发出去，绕过了 get_messages_for_llm 的剥离
"""

from __future__ import annotations

import os
from typing import Any

import pytest

from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.core.llm_service import LLMResult
from fp_core.plugins.task_system import TaskSystemPlugin
from fp_core.taskmap.store import TaskMapStore
from tests.test_entry_hooks import make_agent  # noqa: F401 — pytest fixture 复用

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-modified-messages")

HINT = "\n\n[PERSIST-HINT]"


# ═══════════════════════════════════════════════════════════════
#  ① 写回 _conv + 落盘
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_writeback_to_conv_and_persist(make_agent, monkeypatch, tmp_path):
    """插件改写经 modified_messages 写回 _conv，并随轮末落盘"""
    monkeypatch.chdir(tmp_path)  # 隔离 .fp/tasks.json，避免 task_system 注入干扰
    agent, _ = make_agent()

    async def append_hint(ctx: Any, **kwargs: Any) -> Any:
        # 与 ctx_trim 同款链式：优先在上游插件已写入的修改之上追加
        msgs = ctx.data.get("modified_messages") or list(kwargs.get("messages") or [])
        if not msgs:
            return ctx
        last = msgs[-1]
        msgs[-1] = dict(last)
        msgs[-1]["content"] = (last.get("content") or "") + HINT
        ctx.data["modified_messages"] = msgs
        return ctx

    # priority=150：排在 task_system(50) / ctx_trim_hint(100) 之后，确保不被覆盖
    agent.lifecycle.register(
        LifecycleHook.ON_BEFORE_LLM_CALL,
        append_hint,
        priority=150,
        name="persist_hint",
    )

    await agent.process("hello")

    # ① 内存：写回 _conv（唯一事实源）
    mem = [m.get("content") or "" for m in agent._conv.messages]
    assert sum(HINT in c for c in mem) == 1, "改写未写回 _conv"

    # ② 磁盘：轮末 save_context 落盘，load_context 能读回
    saved = agent.session.load_context(agent._conv.system_prompt)
    disk = [m.get("content") or "" for m in saved]
    assert sum(HINT in c for c in disk) == 1, "改写未落盘"


# ═══════════════════════════════════════════════════════════════
#  ② 同一条末条消息不叠第二份 hint
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_task_hint_idempotent_on_same_last_message(monkeypatch, tmp_path):
    """同一条末条消息被 fire 两次（本轮夭折后重入）不叠第二份 hint

    触发窗口：钩子 fire 后 LLM 报错 continue / 取消 / early-exit——
    没有 assistant 消息落地、末条未换，下一次钩子又对着它。
    正常每轮末条都会换（工具结果/新消息落地），故不影响持续提醒。
    """
    monkeypatch.chdir(tmp_path)  # 隔离 .fp/tasks.json
    os.makedirs(".fp", exist_ok=True)

    lc = LifecycleManager()
    plugin = TaskSystemPlugin()
    plugin.on_register(lc)
    TaskMapStore().create("演示")

    msgs: list[dict[str, Any]] = [
        {"role": "system", "content": "S"},
        {"role": "user", "content": "hi"},
    ]

    # 第一次：正常注入
    ctx1 = await lc.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=msgs, tools=[])
    mod1 = ctx1.data.get("modified_messages")
    assert mod1 is not None, "首次未注入 [task] hint"
    assert mod1[-1]["content"].count("\n\n[task] ") == 1

    # 第二次：以「写回后的状态」为输入（_conv 已含 hint、末条未换）
    ctx2 = await lc.emit(LifecycleHook.ON_BEFORE_LLM_CALL, messages=mod1, tools=[])
    assert ctx2.data.get("modified_messages") is None, "同一条末条消息被重复改写"


# ═══════════════════════════════════════════════════════════════
#  ③ 下划线协议标记不出网
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_underscore_marker_stripped_even_with_modified(make_agent, monkeypatch, tmp_path):
    """设了 modified_messages 时，_ 下划线协议标记也不得出网

    旧路径 messages_for_llm = ctx.data.get("modified_messages", 默认)
    会整段绕过 get_messages_for_llm，于是 _drained（空闲泵忙时注入的人类
    话语标记）被直接发给 LLM。改后统一从 _conv 取，剥离恒生效。
    """
    monkeypatch.chdir(tmp_path)
    agent, _ = make_agent()
    captured: list[dict[str, Any]] = []

    async def mock_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
        captured.extend(messages)
        return LLMResult(
            message={"role": "assistant", "content": "ok", "_interrupted": False},
            usage=None,
        )

    agent._llm.chat = mock_chat

    # 模拟空闲泵忙时注入留下的协议标记（agent.py 环顶 drain 置 _drained）
    agent._conv.append({"role": "user", "content": "忙时注入", "_drained": True})

    async def touch(ctx: Any, **kwargs: Any) -> Any:
        msgs = ctx.data.get("modified_messages") or list(kwargs.get("messages") or [])
        if not msgs:
            return ctx
        msgs[-1] = dict(msgs[-1])
        msgs[-1]["content"] = (msgs[-1].get("content") or "") + "!"
        ctx.data["modified_messages"] = msgs
        return ctx

    agent.lifecycle.register(LifecycleHook.ON_BEFORE_LLM_CALL, touch, priority=150, name="touch")

    await agent.process("hello")

    assert captured, "LLM 未被调用"
    leaked = [m for m in captured if any(k.startswith("_") for k in m)]
    assert not leaked, f"下划线协议标记出网了: {leaked}"
