"""WebUIBridge 连续性校验的**真实 agent 轮次**回归测试。

与 test_webui_bridge.py 的差别：那边用手工 emit 的合成事件验证单点触发，
这边跑完整 ``Agent.process()`` 轮次（stub LLM，无网络），验证判据在真实
事件时序下既不漏报也不误报。

覆盖：
  A. 普通工具轮（工具不改上下文）→ 0 次 history_changed（无假阳性）
  B. 工具在 turn 中途 replace_all 重写（模拟 shortcircuit）→ 恰好 1 次
  C. 纯文本轮（无工具）→ 0 次
  D. 重写造成的孤儿 tool 消息经 ON_CTX_REPAIR 修复 → **不**追加第二次广播

为什么 B 恰好 1 次而不止 1 次：重写删掉 assistant(tool_calls) 后，随后
入库的 tool 消息成为孤儿，引擎的 repair_tool_ordering 会把它合并成一条
system 消息（长度可能回到原值）。桥在 ON_CTX_REPAIR 只同步基线、不广播，
否则一次重写会在前端触发两次 clearUI + 重拉。
"""

from __future__ import annotations

import asyncio
import os
from typing import Any

os.environ.setdefault("LLM_API_KEY", "sk-test-continuity")

from fp_core.core.agent import Agent
from fp_core.core.lifecycle import LifecycleHook
from fp_core.core.llm_service import LLMResult
from fp_core.core.tool_executor import ToolExecutor
from fp_core.tools import ToolRegistry
from fp_webui.bridge import WebUIBridge

_NOOP_SPEC = {
    "type": "function",
    "function": {
        "name": "noop",
        "description": "noop",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


class _RecordingBus:
    """只记录 payload 的总线替身（WebUIBridge 只用到 publish）"""

    def __init__(self) -> None:
        self.payloads: list[dict[str, Any]] = []

    async def publish(self, payload: Any, **_kw: Any) -> None:
        self.payloads.append(payload)

    def history_changed_count(self) -> int:
        return sum(1 for p in self.payloads if p.get("type") == "history_changed")


def _make_agent(executor: Any) -> Agent:
    """带单个 noop 工具的干净 agent（注销用户级审批钩子，避免交互等待）"""
    registry = ToolRegistry()
    registry.register_tool("noop", _NOOP_SPEC, executor=executor)
    agent = Agent(enable_log=False, tool_executor=ToolExecutor(registry=registry))
    agent.lifecycle.unregister(LifecycleHook.ON_TOOL_CALL, "tool_audit_on_tool_call")
    return agent


def _tool_then_text(tool_calls: bool):
    """stub LLM：可选第一轮发起 noop 调用，随后给出终答"""
    calls = {"n": 0}

    async def chat(messages: Any, tools: Any = None, **_overrides: Any) -> LLMResult:
        calls["n"] += 1
        if tool_calls and calls["n"] == 1:
            return LLMResult(
                message={
                    "role": "assistant",
                    "content": "",
                    "tool_calls": [
                        {"id": "call_1", "type": "function", "function": {"name": "noop", "arguments": "{}"}}
                    ],
                    "_interrupted": False,
                },
                usage=None,
            )
        return LLMResult(
            message={"role": "assistant", "content": "done", "_interrupted": False},
            usage=None,
        )

    return chat


def _run_turn(*, use_tool: bool, rewrite_mid_turn: bool) -> _RecordingBus:
    """跑一个完整 agent 轮次，返回记录到的广播"""
    holder: dict[str, Agent] = {}

    async def executor(_params: Any) -> str:
        if rewrite_mid_turn:
            conv = holder["agent"]._conv
            # 模拟 shortcircuit：整体替换上下文（绕过所有 add_* / append 路径）
            conv.replace_all(conv.messages[:2])
        return "ok"

    agent = _make_agent(executor)
    holder["agent"] = agent

    bus = _RecordingBus()
    WebUIBridge(bus).on_register(agent.lifecycle)
    agent._llm.chat = _tool_then_text(use_tool)  # type: ignore[method-assign]

    asyncio.run(agent.process("hello"))
    return bus


def test_plain_tool_turn_no_false_positive():
    """A: 普通工具轮的每条 append 都连续 → 不得广播 history_changed"""
    bus = _run_turn(use_tool=True, rewrite_mid_turn=False)
    assert bus.history_changed_count() == 0


def test_text_only_turn_no_false_positive():
    """C: 纯文本轮同样连续 → 不得广播"""
    bus = _run_turn(use_tool=False, rewrite_mid_turn=False)
    assert bus.history_changed_count() == 0


def test_tool_rewrite_detected_exactly_once():
    """B/D: 工具中途重写 → 检出且仅一次（repair 不追加广播）"""
    bus = _run_turn(use_tool=True, rewrite_mid_turn=True)
    assert bus.history_changed_count() == 1
    changed = [p for p in bus.payloads if p.get("type") == "history_changed"]
    assert isinstance(changed[0].get("msg_count"), int), "history_changed 应携带 msg_count"


def test_no_rewrite_means_user_message_still_sent():
    """重写判据不得干扰既有 user_message 广播语义"""
    bus = _run_turn(use_tool=True, rewrite_mid_turn=True)
    assert any(p.get("type") == "user_message" for p in bus.payloads)
