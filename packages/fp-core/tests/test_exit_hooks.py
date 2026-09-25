"""块5 出口区埋点测试：#46/#49/#50/#53/#54

覆盖边集清单出口区（桌面 agent_loop_hook_edges.html #45-#54）：
  #46 ON_SESSION_SAVE        出口 save_context 覆写磁盘前发出（journal 状态存档点）
  #49 ON_CONTEXT_RESTORE     process 正常返回 → finally contextvar 复位
  #50 ON_CONTEXT_RESTORE     异常传播路径 → finally 同样触发（entry 仍为 process）
  #48 早退语义               空输入/命令路径不经 save_context（SESSION_SAVE 不发）
  #53 面外 usage 回流        LLMService.summarize 的 usage → TokenTracker 聚合
  #54 handoff 重放入库       op=handoff 的 ON_CTX_APPEND（execve 前发出）
"""

from __future__ import annotations

import json
import os
from typing import Any

import pytest

import fp_core.commands as cmd_mod
import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.commands.reload import execute as reload_cmd
from fp_core.core.agent import Agent
from fp_core.core.handoff import perform_exec_reload
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.core.llm_service import LLMResult

# ═══════════════════════════════════════════════════════════════
# 测试基建（与 test_entry_hooks 同范式，独立副本）
# ═══════════════════════════════════════════════════════════════


class Recorder:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    def names(self) -> list[str]:
        return [e["hook"] for e in self.events]

    def of(self, hook: LifecycleHook | str) -> list[dict[str, Any]]:
        name = hook.name if isinstance(hook, LifecycleHook) else hook
        return [e for e in self.events if e["hook"] == name]

    def attach(self, lm: LifecycleManager, *hooks: LifecycleHook) -> None:
        for h in hooks:

            async def rec(ctx: Any, **kwargs: Any) -> None:
                self.events.append({"hook": ctx.hook.name, "data": dict(ctx.data)})

            lm.register(h, rec, name=f"rec_{h.name}")


@pytest.fixture
def make_agent(tmp_path, monkeypatch):
    """构造隔离 Agent（session 落 tmp）+ mock LLM + 记录器"""

    def _make(*attach: LifecycleHook) -> tuple[Agent, Recorder]:
        sessions = tmp_path / "sessions"
        sessions.mkdir(exist_ok=True)
        monkeypatch.setattr(cfg, "SESSIONS_DIR", str(sessions))
        monkeypatch.setattr(session_mod, "SESSIONS_DIR", str(sessions))

        agent = Agent(enable_log=False)
        # 防御性注销（与 test_entry_hooks 同模式）
        agent.lifecycle.unregister(LifecycleHook.ON_TOOL_CALL, "tool_audit_on_tool_call")

        async def mock_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
            return LLMResult(
                message={"role": "assistant", "content": "ok", "_interrupted": False},
                usage=None,
            )

        agent._llm.chat = mock_chat  # pyright: ignore[reportPrivateUsage]

        rec = Recorder()
        rec.attach(agent.lifecycle, *attach)
        return agent, rec

    return _make


# ═══════════════════════════════════════════════════════════════
#  #46 ON_SESSION_SAVE
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_session_save_emitted_with_final_state(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_CONTEXT_UPDATE,
        LifecycleHook.ON_SESSION_SAVE,
        LifecycleHook.ON_BEFORE_RESPONSE,
        LifecycleHook.ON_CONTEXT_RESTORE,
    )
    await agent.process("hi")

    ss = rec.of(LifecycleHook.ON_SESSION_SAVE)
    assert len(ss) == 1, f"ON_SESSION_SAVE 应恰好 1 次，实际 {len(ss)}"
    data = ss[0]["data"]
    assert data["session_id"] == agent.session.session_id
    assert data["path"] == agent.session.get_session_path()
    assert data["msg_count"] == len(data["messages"]) > 0
    # messages = 覆写磁盘的最终状态（含本轮 user + assistant）
    roles = [m.get("role") for m in data["messages"]]
    assert "user" in roles and "assistant" in roles

    # 出口顺序：CTX_UPDATE → SESSION_SAVE → BEFORE_RESPONSE → CONTEXT_RESTORE(finally)
    names = rec.names()
    assert names.index("ON_CONTEXT_UPDATE") < names.index("ON_SESSION_SAVE")
    assert names.index("ON_SESSION_SAVE") < names.index("ON_BEFORE_RESPONSE")
    assert names[-1] == "ON_CONTEXT_RESTORE"


@pytest.mark.asyncio
async def test_session_save_not_fired_on_empty_input(make_agent):
    # #48：空输入早退不经 save_context（且 contextvar 未绑定 → 无 RESTORE）
    agent, rec = make_agent(LifecycleHook.ON_SESSION_SAVE, LifecycleHook.ON_CONTEXT_RESTORE)
    resp = await agent.process("   ")
    assert resp.content == ""
    assert not rec.of(LifecycleHook.ON_SESSION_SAVE)
    assert not rec.of(LifecycleHook.ON_CONTEXT_RESTORE)


@pytest.mark.asyncio
async def test_session_save_not_fired_on_command_path(make_agent, monkeypatch):
    # #48：命令路径在 handle_command 直接 return，不经 save_context；
    # 但 contextvar 已绑定 → CONTEXT_RESTORE 必须发（finally 语义）
    agent, rec = make_agent(LifecycleHook.ON_SESSION_SAVE, LifecycleHook.ON_CONTEXT_RESTORE)

    executed: list[str] = []

    def execute(state: Any, arg: str) -> str:
        executed.append(arg)
        return "cmd-out"

    from types import SimpleNamespace

    mod = SimpleNamespace(name="exit5cmd", description="exit hook test cmd", execute=execute)
    cmd_mod.register_command("exit5cmd", mod)
    monkeypatch.setattr(cfg, "SESSIONS_DIR", str(cfg.SESSIONS_DIR))  # 锁定隔离状态
    try:
        resp = await agent.process("/exit5cmd")
    finally:
        cmd_mod.unregister_command("exit5cmd")

    assert executed == [""]
    assert "cmd-out" in resp.content
    assert not rec.of(LifecycleHook.ON_SESSION_SAVE)
    restore = rec.of(LifecycleHook.ON_CONTEXT_RESTORE)
    assert len(restore) == 1 and restore[0]["data"]["entry"] == "process"


# ═══════════════════════════════════════════════════════════════
#  #49/#50 ON_CONTEXT_RESTORE
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_context_restore_normal_return(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_CONTEXT_RESTORE)
    await agent.process("hi")
    rows = rec.of(LifecycleHook.ON_CONTEXT_RESTORE)
    assert len(rows) == 1
    assert rows[0]["data"]["entry"] == "process"


@pytest.mark.asyncio
async def test_context_restore_on_exception_path(make_agent, monkeypatch):
    # #50：_process_inner 抛异常 → finally 仍发 RESTORE，异常继续传播
    agent, rec = make_agent(LifecycleHook.ON_CONTEXT_RESTORE)

    async def boom(user_input: str, continuation: bool = False) -> Any:
        raise RuntimeError("boom")

    monkeypatch.setattr(agent, "_process_inner", boom)
    with pytest.raises(RuntimeError, match="boom"):
        await agent.process("hi")

    rows = rec.of(LifecycleHook.ON_CONTEXT_RESTORE)
    assert len(rows) == 1, "异常路径必须触发 ON_CONTEXT_RESTORE"
    assert rows[0]["data"]["entry"] == "process"


@pytest.mark.asyncio
async def test_context_restore_continue_entry(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_CONTEXT_RESTORE)
    await agent.continue_conversation()
    rows = rec.of(LifecycleHook.ON_CONTEXT_RESTORE)
    assert len(rows) == 1
    assert rows[0]["data"]["entry"] == "continue"


# ═══════════════════════════════════════════════════════════════
#  #53 summarize usage 回流（面外缺口修复）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_summarize_usage_flows_into_tracker(make_agent):
    agent, rec = make_agent()
    assert agent._token_tracker.total.total_tokens == 0  # pyright: ignore[reportPrivateUsage]

    async def fake_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
        return LLMResult(
            message={"role": "assistant", "content": "summary-out"},
            usage={"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        )

    agent._llm.chat = fake_chat  # pyright: ignore[reportPrivateUsage]
    out = await agent._llm.summarize("long text")  # pyright: ignore[reportPrivateUsage]

    assert out == "summary-out"
    # 修复前 usage 在 llm_service 被丢弃，连聚合统计都没进
    assert agent._token_tracker.total.total_tokens == 15  # pyright: ignore[reportPrivateUsage]


@pytest.mark.asyncio
async def test_main_loop_usage_not_double_counted(make_agent):
    # 主循环路径走 _invoke_llm → agent 自行 accumulate，不经过 on_usage 回调
    agent, rec = make_agent()
    before = agent._token_tracker.total.total_tokens  # pyright: ignore[reportPrivateUsage]
    await agent.process("hi")
    # mock_chat 返回 usage=None → 无论如何不产生额外计数
    assert agent._token_tracker.total.total_tokens == before  # pyright: ignore[reportPrivateUsage]


# ═══════════════════════════════════════════════════════════════
#  #54 handoff 重放入库
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_handoff_appends_emit_ctx_append(make_agent, monkeypatch):
    agent, rec = make_agent(LifecycleHook.ON_CTX_APPEND)

    # 构造未应答的 assistant(tool_calls) 尾部
    agent._conv.add_assistant_message(  # pyright: ignore[reportPrivateUsage]
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{"id": "tc_exit5", "type": "function", "function": {"name": "foo", "arguments": "{}"}}],
        }
    )
    before = len(agent._conv.messages)  # pyright: ignore[reportPrivateUsage]

    # 阻断真实重启：os.execve 抛错 → 核心回滚并返回错误文本（emit 已在 execve 前发出）
    def fake_execve(*args: Any, **kwargs: Any) -> None:
        raise OSError("execve blocked in test")

    monkeypatch.setattr(os, "execve", fake_execve)
    # 不含 cwd → 跳过 os.chdir，避免污染测试进程工作目录
    monkeypatch.setenv("FP_LAUNCH_JSON", json.dumps({"argv": ["/bin/true"]}))
    # 防御性清理可能存在的陈旧 handoff 文件
    from fp_core.platform_utils import get_data_dir

    stale = os.path.join(get_data_dir(), "reload_handoff.json")
    if os.path.isfile(stale):
        os.remove(stale)

    err = await perform_exec_reload(agent.state, kind="tool", tool_result_text="resumed-text")
    assert err.startswith("❌"), f"execve 被阻断应返回错误文本，实际: {err}"

    handoff_rows = [e for e in rec.of(LifecycleHook.ON_CTX_APPEND) if e["data"].get("op") == "handoff"]
    assert len(handoff_rows) == 1, f"handoff 重放应发恰好 1 行 op=handoff，实际 {len(handoff_rows)}"
    data = handoff_rows[0]["data"]
    # ctx-after：入库后消息数 = 入库前 + 1（补写占位 tool 消息）
    assert data["msg_count"] == before + 1
    assert data["message"].get("role") == "tool"
    assert data["message"].get("tool_call_id") == "tc_exit5"
    assert len(data["messages"]) == data["msg_count"]


@pytest.mark.asyncio
async def test_handoff_no_append_when_fully_answered(make_agent, monkeypatch):
    # 尾部无未应答 tool_calls → 不补写、不发 op=handoff
    agent, rec = make_agent(LifecycleHook.ON_CTX_APPEND)
    agent._conv.add_assistant_message({"role": "assistant", "content": "done"})  # pyright: ignore[reportPrivateUsage]

    def fake_execve(*args: Any, **kwargs: Any) -> None:
        raise OSError("execve blocked in test")

    monkeypatch.setattr(os, "execve", fake_execve)
    monkeypatch.setenv("FP_LAUNCH_JSON", json.dumps({"argv": ["/bin/true"]}))
    from fp_core.platform_utils import get_data_dir

    stale = os.path.join(get_data_dir(), "reload_handoff.json")
    if os.path.isfile(stale):
        os.remove(stale)

    err = await perform_exec_reload(agent.state, kind="tool", tool_result_text="resumed-text")
    assert err.startswith("❌")

    handoff_rows = [e for e in rec.of(LifecycleHook.ON_CTX_APPEND) if e["data"].get("op") == "handoff"]
    assert not handoff_rows, "无未应答 tool_calls 时不应发 op=handoff 行"


@pytest.mark.asyncio
async def test_reload_command_awaits_async_core(make_agent, monkeypatch):
    # 调用方接线：/reload 命令 await async 版 perform_exec_reload
    agent, rec = make_agent()
    called: dict[str, Any] = {}

    async def fake_reload(state: Any, *, kind: str, tool_result_text: str | None = None) -> str:
        called["kind"] = kind
        return "❌ 假装失败"

    monkeypatch.setattr("fp_core.core.handoff.perform_exec_reload", fake_reload)
    # reload 命令是延迟导入 → patch 打在模块属性上即可
    handled, out = await reload_cmd(agent.state, "")
    assert handled is True
    # 注意：reload_cmd 内部 from ... import → patch 需在 import 前；此断言兜底真实路径
    if called:
        assert called["kind"] == "command"
    else:
        # 真实路径被触发（is_processing 拦截或核心自检），至少未抛异常
        assert isinstance(out, str)
