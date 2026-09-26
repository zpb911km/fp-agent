"""复审补缺（A 类）行为测试 — agent loop 端到端

覆盖边：
  #6b ON_COMMAND_BLOCKED      命令守卫分支的 post 出口（与 ON_MESSAGE_BLOCKED 对称）
  #13b ON_CANCEL(stage=loop_top)  环顶中断真正抛出时的事实记录（与 LLM 侧 ON_CANCEL 对称）
  /resume 目标会话落盘          启动按 meta.updated 取最新会话，不落盘 → 崩溃窗口内回退到旧会话

策略同 test_entry_hooks：真实 Agent + mock LLM + 隔离 session 目录。
"""

import asyncio
import json
import os
from types import SimpleNamespace
from typing import Any

import pytest

import fp_core.commands as cmd_mod
import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.agent import Agent
from fp_core.core.lifecycle import LifecycleHook, LifecycleManager
from fp_core.core.llm_service import LLMResult

os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-lifecycle-gaps")


# ═══════════════════════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════════════════════


class Recorder:
    """按触发顺序记录 {hook, data}"""

    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

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
        agent.lifecycle.unregister(LifecycleHook.ON_TOOL_CALL, "tool_audit_on_tool_call")

        async def mock_chat(messages: Any, tools: Any = None, **kw: Any) -> LLMResult:
            return LLMResult(
                message={"role": "assistant", "content": "ok", "_interrupted": False},
                usage=None,
            )

        agent._llm.chat = mock_chat

        rec = Recorder()
        rec.attach(agent.lifecycle, *attach)
        return agent, rec

    return _make


def _register_dyn_command(name: str, output: str = "dyn-out"):
    """注册动态命令，返回注销函数"""
    executed: list[str] = []

    def execute(state: Any, arg: str) -> str:
        executed.append(arg)
        return f"{output}:{arg}" if arg else output

    mod = SimpleNamespace(name=name, description="gap test cmd", execute=execute)
    cmd_mod.register_command(name, mod)
    return executed, (lambda: cmd_mod.unregister_command(name))


# ═══════════════════════════════════════════════════════════════
#  #6b ON_COMMAND_BLOCKED
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_command_blocked_emits_post_hook(make_agent):
    """守卫阻断命令 → ON_COMMAND_BLOCKED 补发，ON_COMMAND 不发，命令体不执行"""
    agent, rec = make_agent(
        LifecycleHook.ON_BEFORE_COMMAND,
        LifecycleHook.ON_COMMAND_BLOCKED,
        LifecycleHook.ON_COMMAND,
    )

    async def blocker(ctx: Any, **kw: Any) -> None:
        ctx.data["blocked"] = True
        ctx.data["block_reason"] = "禁止执行该命令"

    agent.lifecycle.register(LifecycleHook.ON_BEFORE_COMMAND, blocker, name="blocker")
    executed, cleanup = _register_dyn_command("gapblock", output="should-not-run")
    try:
        res = await agent.process("/gapblock 参数x")
    finally:
        cleanup()

    assert "禁止执行该命令" in res.content
    assert res.metadata.get("from_command") is True
    assert executed == [], "被阻断的命令体不应执行"

    blocked = rec.of(LifecycleHook.ON_COMMAND_BLOCKED)
    assert len(blocked) == 1, "阻断路径必须补发 post 出口"
    data = blocked[0]["data"]
    assert data["name"] == "/gapblock"
    assert data["arg"] == "参数x"
    assert data["block_reason"] == "禁止执行该命令"
    assert isinstance(data.get("messages"), list), "post 出口应带 ctx（journal 行）"

    assert rec.of(LifecycleHook.ON_COMMAND) == [], "阻断路径不应发 ON_COMMAND"


@pytest.mark.asyncio
async def test_command_not_blocked_still_only_on_command(make_agent):
    """回归：无守卫时仍走 ON_COMMAND，不误发 ON_COMMAND_BLOCKED"""
    agent, rec = make_agent(LifecycleHook.ON_COMMAND_BLOCKED, LifecycleHook.ON_COMMAND)
    executed, cleanup = _register_dyn_command("gapok", output="ran")
    try:
        res = await agent.process("/gapok a")
    finally:
        cleanup()

    assert "ran:a" in res.content
    assert executed == ["a"]
    assert rec.of(LifecycleHook.ON_COMMAND_BLOCKED) == []
    assert len(rec.of(LifecycleHook.ON_COMMAND)) == 1


# ═══════════════════════════════════════════════════════════════
#  #13b ON_CANCEL(stage="loop_top")
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_loop_top_interrupt_emits_cancel(make_agent):
    """环顶中断：ON_INTERRUPT（pre）+ ON_CANCEL(stage=loop_top)（post），CancelledError 照旧抛出"""
    agent, rec = make_agent(LifecycleHook.ON_INTERRUPT, LifecycleHook.ON_CANCEL)
    agent._interrupted = True

    with pytest.raises(asyncio.CancelledError):
        await agent.process("hi")

    assert len(rec.of(LifecycleHook.ON_INTERRUPT)) == 1, "pre 边应保留"
    cancels = rec.of(LifecycleHook.ON_CANCEL)
    loop_top = [c for c in cancels if c["data"].get("stage") == "loop_top"]
    assert len(loop_top) == 1, f"环顶抛出应记 ON_CANCEL(stage=loop_top)，实际 {cancels}"
    assert loop_top[0]["data"]["reason"] == "用户中断"


@pytest.mark.asyncio
async def test_no_interrupt_no_loop_top_cancel(make_agent):
    """回归：无中断时不得出现 stage=loop_top 的 ON_CANCEL"""
    agent, rec = make_agent(LifecycleHook.ON_INTERRUPT, LifecycleHook.ON_CANCEL)
    await agent.process("正常一轮")
    assert rec.of(LifecycleHook.ON_INTERRUPT) == []
    assert [c for c in rec.of(LifecycleHook.ON_CANCEL) if c["data"].get("stage") == "loop_top"] == []


# ═══════════════════════════════════════════════════════════════
#  /resume 落盘（崩溃窗口）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_resume_persists_target_session(make_agent):
    """/resume 切换后目标会话必须落盘：否则其 meta.updated 落后于刚 save 的旧会话，
    启动扫描（_find_latest_session 取 updated 最新）会回退到旧会话，本次切换静默失效"""
    agent, _rec = make_agent()

    # 旧（当前）会话：有内容，save 后 updated 为当前时刻
    agent._conv.add_user_message("A 的消息")
    agent.session.save_context(agent._conv.to_serializable())

    # 目标会话 B：造一个 updated 很旧的落盘文件
    b_sid = "s_test_resume_target"
    b_path = session_mod._session_path(b_sid)
    stale = {"id": b_sid, "updated": "2000-01-01 00:00:00", "message_count": 1, "summary": "目标会话"}
    with open(b_path, "w", encoding="utf-8") as f:
        f.write(json.dumps(stale, ensure_ascii=False) + "\n")
        f.write(json.dumps({"role": "user", "content": "目标会话历史"}, ensure_ascii=False) + "\n")

    res = await agent.process(f"/resume {b_sid}")

    assert "已切换到会话" in res.content
    assert agent.session.session_id == b_sid, "会话指针应切到目标会话"

    meta_after = session_mod._read_meta_from_file(b_path)
    assert meta_after is not None
    assert meta_after["updated"] != stale["updated"], "目标会话 updated 未刷新 → 重启会回退到旧会话"

    # 磁盘内容 == 切换后的内存 ctx（落盘的是切换后的 ctx，而非旧会话内容）
    serialized = agent._conv.to_serializable()
    non_system = [m for m in serialized if m.get("role") != "system"]
    assert meta_after["message_count"] == len(non_system)
    with open(b_path, encoding="utf-8") as f:
        lines = f.read().strip().splitlines()
    disk_msgs = [json.loads(x) for x in lines[1:]]
    assert disk_msgs == non_system
