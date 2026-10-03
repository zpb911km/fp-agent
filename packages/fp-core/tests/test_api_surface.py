"""唯一接口组的强制约束测试

两部分：
1. 前端导入白名单 —— fp-terminal / fp-webui / fp-acp / fp(路由) 的源码中，
   ``import fp_core...`` 只允许白名单模块（协议 §5）。违规即红。
2. api 行为 —— 两域判据（run 须实例、ctl 生命周期幂等）、ExitReason 映射、
   take_reload 空 handoff、会话用例、事件目录冻结。
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
FRONTEND_DIRS = [
    REPO_ROOT / "packages" / "fp-terminal" / "src",
    REPO_ROOT / "packages" / "fp-webui" / "src",
    REPO_ROOT / "packages" / "fp-acp" / "src",
    REPO_ROOT / "packages" / "fp" / "src",
]

# 协议 §5：实例通信只许 fp_core.api；其余为纯工具模块
ALLOWED = {
    "fp_core",
    "fp_core.api",
    "fp_core.logger",
    "fp_core.config",
    "fp_core.platform_utils",
}

# 匹配 import 语句的模块路径（from fp_core.x import ... / import fp_core.x）
_IMPORT_RE = re.compile(r"^\s*(?:from|import)\s+(fp_core[\w.]*)")


def _violations() -> list[str]:
    found: list[str] = []
    for root in FRONTEND_DIRS:
        if not root.is_dir():
            continue
        for py in root.rglob("*.py"):
            for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
                m = _IMPORT_RE.match(line)
                if not m:
                    continue
                mod = m.group(1)
                if not _is_allowed(mod):
                    rel = py.relative_to(REPO_ROOT)
                    found.append(f"{rel}:{lineno}: 禁止导入 {mod}（应经 fp_core.api）")
    return found


def _is_allowed(mod: str) -> bool:
    for a in ALLOWED:
        if a == "fp_core":
            # 根包只允许精确导入（from fp_core import config / __version__），
            # 不得作为前缀放行 fp_core.core.* 等子模块
            if mod == "fp_core":
                return True
        elif mod == a or mod.startswith(a + "."):
            return True
    return False


def test_frontend_import_whitelist():
    """前端导入白名单（协议 §5）—— 违规即红，见 docs/dev/唯一接口组协议.md"""
    if not any(d.is_dir() for d in FRONTEND_DIRS):
        pytest.skip("非仓库布局（前端包不存在）")
    violations = _violations()
    assert not violations, "前端越界导入 fp_core 内部模块：\n" + "\n".join(violations)


def test_core_does_not_import_frontends():
    """反向约束：core 不得导入任何前端包（分层方向固定 core ← 前端）"""
    core_src = REPO_ROOT / "packages" / "fp-core" / "src"
    if not core_src.is_dir():
        pytest.skip("非仓库布局")
    banned = ("fp_webui", "fp_acp", "fp_cli")
    found: list[str] = []
    for py in core_src.rglob("*.py"):
        for lineno, line in enumerate(py.read_text(encoding="utf-8").splitlines(), 1):
            m = _IMPORT_RE.match(line)
            if m and any(m.group(1).split(".")[0] == b or m.group(1).startswith(b + ".") for b in banned):
                found.append(f"{py.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")
    assert not found, "core 反向依赖前端：\n" + "\n".join(found)


# ══════════════════════════════════════════════════════════════
# api 行为
# ══════════════════════════════════════════════════════════════


class TestTwoPlaneContract:
    """两域判据：实例不存在时 run.* 无意义 → InstanceNotOpenError；ctl 幂等"""

    def test_run_status_requires_open(self):
        from fp_core.api import InstanceNotOpenError, portal

        assert not portal.is_open
        with pytest.raises(InstanceNotOpenError):
            _ = portal.run.status

    def test_run_transcript_requires_open(self):
        from fp_core.api import InstanceNotOpenError, portal

        with pytest.raises(InstanceNotOpenError):
            _ = portal.run.transcript

    def test_ctl_close_idempotent_when_not_open(self):
        import asyncio

        from fp_core.api import portal

        # 未开启时 close 是 no-op（finally 兜底路径依赖此语义）
        asyncio.run(portal.ctl.close())
        assert not portal.is_open

    def test_ctl_open_twice_raises(self):
        import asyncio

        from fp_core.api import Portal, PortalError

        p = Portal()  # 独立实例，避免污染进程单例

        async def scenario():
            await p.ctl.open(enable_log=False)
            assert p.is_open
            with pytest.raises(PortalError):
                await p.ctl.open(enable_log=False)
            await p.ctl.close()

        asyncio.run(scenario())
        assert not p.is_open

    def test_commands_catalog_needs_no_instance(self):
        from fp_core.api import portal

        # 静态命令目录：实例未开启也可列出（补全/ACP 注册场景）
        cmds = portal.run.commands
        assert "help" in cmds and isinstance(cmds["help"], str)


class TestReloadDirective:
    def test_empty_handoff_returns_empty_directive(self):
        import asyncio
        import os

        from fp_core.api import Portal

        os.environ.pop("FP_RELOAD_HANDOFF", None)
        p = Portal()

        async def scenario():
            await p.ctl.open(enable_log=False)
            try:
                return p.ctl.take_reload()
            finally:
                await p.ctl.close()

        d = asyncio.run(scenario())
        assert d.should_continue is False
        assert d.notice is None


class TestReplyRouting:
    """run.reply 经活跃通道转发（ask 带内应答，协议 §2）"""

    def test_reply_without_channel_returns_false(self):
        from fp_core.api import Portal

        p = Portal()
        # 无活跃通道 → False（调用方按普通消息处理）
        assert p.run.reply("任意文本") is False

    def test_send_registers_active_io_for_reply(self):
        """send(io=) 必须登记活跃通道，否则 reply 恒 False（回归）"""
        import asyncio

        from fp_core.api import IOChannel, Portal, Response

        class _AskIO(IOChannel):
            """记录 reply 调用的测试通道"""

            def __init__(self):
                self.replied: tuple[str, str | None] | None = None

            def reply(self, text: str, ask_id: str | None = None) -> bool:
                self.replied = (text, ask_id)
                return True

        p = Portal()
        io = _AskIO()

        async def scenario():
            await p.ctl.open(enable_log=False)
            try:
                # 打桩 process（不打桩 portal 登记路径）
                from fp_core.core.agent import Agent

                orig = Agent.process

                async def fake(self, user_input, io=None):
                    return Response(content="ok")

                Agent.process = fake  # type: ignore[method-assign]
                try:
                    await p.run.send("hi", io=io)
                finally:
                    Agent.process = orig  # type: ignore[method-assign]
                # 实例开启期间断言（close 会清空活跃通道，属设计行为）
                assert p.run.reply("回答", ask_id="a1") is True
                assert io.replied == ("回答", "a1")
            finally:
                await p.ctl.close()

        asyncio.run(scenario())
        # 关闭后 reply 应安全返回 False（无活跃通道）
        assert p.run.reply("迟到的回答") is False


class TestEventCatalog:
    def test_event_types_frozen_catalog(self):
        """事件目录冻结（协议 §4：新增只做加法，不得删除既有类型）"""
        from fp_core.api import EVENT_TYPES

        required = {
            "llm_start",
            "llm_end",
            "tool_select",
            "tool_call",
            "tool_result",
            "error",
            "shutdown",
            "info",
            "warning",
            "thinking",
            "chunk",
            "stream_end",
            "ask",
        }
        assert required <= EVENT_TYPES, f"缺失事件类型: {required - set(EVENT_TYPES)}"

    def test_subscribe_and_cancel(self):
        import asyncio

        from fp_core.api import portal

        received: list[dict] = []

        async def handler(ev):
            received.append(ev)

        async def scenario():
            sub = portal.subscribe(handler)
            await portal.events.publish({"type": "info", "content": "hi"})
            await asyncio.sleep(0.05)  # 泵任务调度
            sub.cancel()

        asyncio.run(scenario())
        assert received and received[0]["type"] == "info"
        assert "seq" in received[0]


class TestExitReason:
    def test_reason_mapping_to_state_flags(self):
        """ExitReason → 内部标志映射（协议 §3 表）"""
        from fp_core.api import ExitReason

        assert ExitReason.FINAL.value == "final"
        assert ExitReason.RECYCLE.value == "recycle"
        assert ExitReason.DISCARD.value == "discard"
        assert len(ExitReason) == 3  # 枚举封闭，新增需改协议


class TestSessionOps:
    """会话用例（样板归核后的唯一实现）"""

    @pytest.fixture()
    def state(self):
        """最小 State：只带会话用例触碰的字段"""
        import tempfile

        import fp_core.core.session as session_mod
        from fp_core.core.conversation import ConversationState
        from fp_core.core.session import SessionManager
        from fp_core.core.state import State

        with tempfile.TemporaryDirectory() as tmp:
            orig = session_mod.SESSIONS_DIR
            session_mod.SESSIONS_DIR = tmp
            try:
                sm = SessionManager()
                st = State(
                    conversation=ConversationState("BASE PROMPT"),
                    session=sm,
                    llm=None,  # type: ignore[arg-type]
                    lifecycle=None,  # type: ignore[arg-type]
                    plugins=None,  # type: ignore[arg-type]
                    tool_exec=None,  # type: ignore[arg-type]
                    io=None,  # type: ignore[arg-type]
                    token_tracker=None,  # type: ignore[arg-type]
                )
                yield st
            finally:
                session_mod.SESSIONS_DIR = orig

    def test_fork_new_switch_roundtrip(self, state):
        from fp_core.core.session_ops import fork_new, switch_to

        # 制造有历史的当前会话
        state.conversation.append({"role": "user", "content": "你好世界测试消息"})
        state.conversation.append({"role": "assistant", "content": "答复"})

        info1 = fork_new(state)
        assert info1.session_id != info1.previous_sid
        assert state.conversation.get_non_system_messages() == []  # 新会话已清空

        # 切回旧会话 → 消息恢复，system prompt 保留（rebuild 走 PromptBuilder）
        loaded = switch_to(state, info1.previous_sid)
        assert loaded is not None
        msgs = state.conversation.messages
        assert msgs[0]["role"] == "system" and msgs[0]["content"]
        assert len(state.conversation.get_non_system_messages()) == 2

        # 切到不存在的会话 → None（前端决定 404/自动新建）
        assert switch_to(state, "s_nonexistent") is None

    def test_rebuild_replays_prompt_appends(self, state):
        """重建后插件注入不丢失（原 webui 独有补丁，现归核）"""
        from fp_core.core.session_ops import fork_new

        state.prompt_appends = ["【插件注入】说明段"]
        fork_new(state)
        sp = state.conversation.system_prompt
        assert "【插件注入】说明段" in sp
        assert sp.endswith("【插件注入】说明段")  # 注入段在重建后仍追加于末尾

    def test_clear_resets_and_replays(self, state):
        from fp_core.core.session_ops import clear

        state.prompt_appends = ["注入X"]
        state.conversation.append({"role": "user", "content": "残余消息"})
        clear(state)
        assert state.conversation.get_non_system_messages() == []
        assert "注入X" in state.conversation.system_prompt

    def test_read_messages_missing_file(self, state):
        from fp_core.core.session_ops import read_messages

        assert read_messages("s_does_not_exist") is None
