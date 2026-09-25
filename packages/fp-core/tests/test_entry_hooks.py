"""块2 — 入口区埋点 #1–#12 行为测试（agent loop 端到端）

覆盖边：#2 ON_INITIALIZED / #3 ON_EMPTY / #4 ON_INPUT / #5 ON_BEFORE_COMMAND
        #6 ON_COMMAND / #7 ON_FALLTHROUGH / #8 ON_MSG_ENTER
        #9 ON_MESSAGE_BLOCKED（source=filter|received）/ #11 ON_CTX_APPEND(op=user)
        #12 ON_RESUME

策略：真实 Agent + mock `_llm.chat`（chat_stream 自动降级，见 llm_service.py:152），
      注册记录钩子后走完整 process() 路径，断言触发集合/顺序/payload。
"""

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

# ── LLM API KEY 过门禁（Agent 构造时 check_llm_config） ──
os.environ.setdefault("LLM_API_KEY", "sk-test-key-for-entry-hooks")


# ═══════════════════════════════════════════════════════════════
#  工具
# ═══════════════════════════════════════════════════════════════


class Recorder:
    """按触发顺序记录 {hook, data}"""

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
        # 防御性注销（与 test_agent_tool_parallel 同模式）
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


def _register_dyn_command(name: str, executed: list[str] | None = None, output: str = "dyn-out"):
    """注册动态命令，返回注销函数"""

    def execute(state: Any, arg: str) -> str:
        if executed is not None:
            executed.append(arg)
        return f"{output}:{arg}" if arg else output

    mod = SimpleNamespace(name=name, description="entry hook test cmd", execute=execute)
    cmd_mod.register_command(name, mod)

    def _cleanup() -> None:
        cmd_mod.unregister_command(name)

    return _cleanup


# ═══════════════════════════════════════════════════════════════
#  #1/#2 初始化
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_initialized_first_then_idempotent(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_INITIALIZED, LifecycleHook.ON_INIT)
    await agent.process("")
    await agent.process("")
    assert rec.names() == ["ON_INIT", "ON_INITIALIZED", "ON_INITIALIZED"]
    inited = rec.of(LifecycleHook.ON_INITIALIZED)
    assert [e["data"]["first_time"] for e in inited] == [True, False]
    # ON_INIT 只在首次发
    assert len(rec.of(LifecycleHook.ON_INIT)) == 1


# ═══════════════════════════════════════════════════════════════
#  #3 空输入 / #4 输入到达
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_empty_input(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_EMPTY, LifecycleHook.ON_INPUT)
    resp = await agent.process("   ")
    assert resp.content == ""
    assert rec.names() == ["ON_EMPTY"]
    assert rec.of(LifecycleHook.ON_EMPTY)[0]["data"]["content"] == "   "
    assert isinstance(rec.of(LifecycleHook.ON_EMPTY)[0]["data"]["messages"], list)


@pytest.mark.asyncio
async def test_on_input_fires_for_normal_message(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_INPUT,
        LifecycleHook.ON_MSG_ENTER,
        LifecycleHook.ON_COMMAND,
    )
    resp = await agent.process("hello")
    assert resp.content == "ok"
    # ON_INPUT 先于 ON_MSG_ENTER，命令钩子不触发
    assert rec.names() == ["ON_INPUT", "ON_MSG_ENTER"]
    assert rec.of(LifecycleHook.ON_INPUT)[0]["data"]["content"] == "hello"


# ═══════════════════════════════════════════════════════════════
#  #5/#6/#7 命令链
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_command_hooks_full_chain(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_INPUT,
        LifecycleHook.ON_BEFORE_COMMAND,
        LifecycleHook.ON_COMMAND,
        LifecycleHook.ON_FALLTHROUGH,
        LifecycleHook.ON_MSG_ENTER,
        LifecycleHook.ON_CTX_APPEND,
    )
    cleanup = _register_dyn_command("dync")
    try:
        resp = await agent.process("/dync alpha")
    finally:
        cleanup()

    assert resp.content == "dyn-out:alpha"
    assert resp.metadata.get("from_command") is True
    assert rec.names() == ["ON_INPUT", "ON_BEFORE_COMMAND", "ON_COMMAND"]

    bc = rec.of(LifecycleHook.ON_BEFORE_COMMAND)[0]["data"]
    assert bc["name"] == "/dync" and bc["arg"] == "alpha"

    cmd = rec.of(LifecycleHook.ON_COMMAND)[0]["data"]
    assert cmd["handled"] is True
    assert cmd["output"] == "dyn-out:alpha"
    assert cmd["latency_ms"] >= 0
    assert isinstance(cmd["messages"], list)  # journal ctx-after

    # 命令早退：不降级、不入 ctx
    assert not rec.of(LifecycleHook.ON_FALLTHROUGH)
    assert not rec.of(LifecycleHook.ON_MSG_ENTER)
    assert not rec.of(LifecycleHook.ON_CTX_APPEND)


@pytest.mark.asyncio
async def test_on_before_command_blocked(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_BEFORE_COMMAND,
        LifecycleHook.ON_COMMAND,
    )
    executed: list[str] = []
    cleanup = _register_dyn_command("dynb", executed=executed)

    def blocker(ctx: Any, **kwargs: Any) -> None:
        ctx.data["blocked"] = True
        ctx.data["block_reason"] = "插件拦了"

    agent.lifecycle.register(LifecycleHook.ON_BEFORE_COMMAND, blocker, name="blk")
    try:
        resp = await agent.process("/dynb x")
    finally:
        cleanup()

    assert resp.content == "插件拦了"
    assert resp.metadata.get("from_command") is True
    assert executed == [], "blocked 后命令不得执行"
    assert not rec.of(LifecycleHook.ON_COMMAND), "未执行则无 post 观察点"


@pytest.mark.asyncio
async def test_unhandled_slash_falls_through_to_message(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_BEFORE_COMMAND,
        LifecycleHook.ON_COMMAND,
        LifecycleHook.ON_FALLTHROUGH,
        LifecycleHook.ON_MSG_ENTER,
        LifecycleHook.ON_CTX_APPEND,
    )
    resp = await agent.process("/no_such_cmd_ever")
    assert resp.content == "ok"  # 降级为消息 → LLM mock 回复

    assert rec.names() == [
        "ON_BEFORE_COMMAND",
        "ON_COMMAND",
        "ON_FALLTHROUGH",
        "ON_CTX_APPEND",  # op=user（降级入库）
        "ON_CTX_APPEND",  # op=assistant（块3：LLM 回复入库）
    ]
    assert rec.of(LifecycleHook.ON_COMMAND)[0]["data"]["handled"] is False
    # fallthrough 路径不发 ON_MSG_ENTER（#7 与 #8 是两条互斥边）
    assert not rec.of(LifecycleHook.ON_MSG_ENTER)
    # 降级后仍入库
    ca = rec.of(LifecycleHook.ON_CTX_APPEND)[0]["data"]
    assert ca["op"] == "user"
    assert ca["message"]["content"] == "/no_such_cmd_ever"
    # 块3 #27：assistant 入库后也有观察点
    assert rec.of(LifecycleHook.ON_CTX_APPEND)[1]["data"]["op"] == "assistant"


# ═══════════════════════════════════════════════════════════════
#  #9 消息拦截（两 source）
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_message_blocked_at_filter(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_MESSAGE_BLOCKED,
        LifecycleHook.ON_MESSAGE_RECEIVED,
        LifecycleHook.ON_CTX_APPEND,
    )

    def blocker(ctx: Any, **kwargs: Any) -> None:
        ctx.data["blocked"] = True
        ctx.data["block_reason"] = "filter 拒了"

    agent.lifecycle.register(LifecycleHook.ON_MESSAGE_FILTER, blocker, name="blk")
    resp = await agent.process("hi")

    assert resp.content == "filter 拒了"
    blk = rec.of(LifecycleHook.ON_MESSAGE_BLOCKED)[0]["data"]
    assert blk["source"] == "filter"
    assert blk["content"] == "hi"
    assert blk["block_reason"] == "filter 拒了"
    # 拦在 FILTER：RECEIVED 不发、不入库
    assert not rec.of(LifecycleHook.ON_MESSAGE_RECEIVED)
    assert not rec.of(LifecycleHook.ON_CTX_APPEND)


@pytest.mark.asyncio
async def test_message_blocked_at_received(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_MESSAGE_BLOCKED,
        LifecycleHook.ON_CTX_APPEND,
    )

    def blocker(ctx: Any, **kwargs: Any) -> None:
        ctx.data["blocked"] = True
        ctx.data["block_reason"] = "received 拒了"

    agent.lifecycle.register(LifecycleHook.ON_MESSAGE_RECEIVED, blocker, name="blk")
    resp = await agent.process("hi")

    assert resp.content == "received 拒了"
    blk = rec.of(LifecycleHook.ON_MESSAGE_BLOCKED)[0]["data"]
    assert blk["source"] == "received"
    assert blk["content"] == "hi"
    # 拦在 RECEIVED：不入库
    assert not rec.of(LifecycleHook.ON_CTX_APPEND)


# ═══════════════════════════════════════════════════════════════
#  #11 user 入库 post 观察点
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_ctx_append_user_payload(make_agent):
    agent, rec = make_agent(LifecycleHook.ON_CTX_APPEND)
    await agent.process("say hi")

    user_rows = [e for e in rec.of(LifecycleHook.ON_CTX_APPEND) if e["data"]["op"] == "user"]
    assert len(user_rows) == 1
    ca = user_rows[0]["data"]
    assert ca["message"] == {"role": "user", "content": "say hi"}
    # ctx-after：msg_count 与 messages 末条一致指向刚入库的消息
    assert ca["msg_count"] == len(ca["messages"])
    assert ca["messages"][-1]["content"] == "say hi"


# ═══════════════════════════════════════════════════════════════
#  #12 handoff 续接
# ═══════════════════════════════════════════════════════════════


@pytest.mark.asyncio
async def test_on_resume_skips_entry_hooks(make_agent):
    agent, rec = make_agent(
        LifecycleHook.ON_RESUME,
        LifecycleHook.ON_INPUT,
        LifecycleHook.ON_MSG_ENTER,
        LifecycleHook.ON_MESSAGE_FILTER,
    )
    agent._conv.add_user_message("prior")
    resp = await agent.continue_conversation()

    assert resp.content == "ok"
    assert rec.names() == ["ON_RESUME"]
    assert isinstance(rec.of(LifecycleHook.ON_RESUME)[0]["data"]["messages"], list)
