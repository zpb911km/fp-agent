"""Zeta 插件装配测试（P1）。"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

from fp_core.plugins.zeta.discovery import derive_instance_name
from fp_core.plugins.zeta.plugin import (
    PEER_ANSWER_TOOL,
    PEER_LIST_TOOL,
    PEER_SEND_TOOL,
    ZETA_DESCRIPTION,
    ZetaPlugin,
)


def _plant_holder(ws: Path, sid: str, *, pid: int) -> None:
    """手工落一条"同名登记"——模拟同 workspace 里已被别的实例占着。"""
    d = ws / ".fp" / "zeta" / "instances"
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{derive_instance_name(ws, sid)}.json").write_text(
        json.dumps({"name": derive_instance_name(ws, sid), "sid": sid, "pid": pid, "workspace": str(ws)}),
        encoding="utf-8",
    )


class FakeRegistry:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def register_tool(self, name: str, definition: Any, executor: Any) -> None:
        self.tools[name] = executor

    def unregister_tool(self, name: str) -> None:
        self.tools.pop(name, None)


class FakeCtx:
    def __init__(self) -> None:
        self.data: dict[str, Any] = {}


class FakeState:
    def __init__(self, sid: str) -> None:
        self.session_id = sid


def test_tool_definitions_wellformed():
    cases = ((PEER_LIST_TOOL, "peer_list"), (PEER_SEND_TOOL, "peer_send"), (PEER_ANSWER_TOOL, "peer_answer"))
    for tool, name in cases:
        assert tool["type"] == "function"
        assert tool["function"]["name"] == name
        assert tool["function"]["parameters"]["type"] == "object"


def test_disabled_does_not_register(monkeypatch):
    monkeypatch.setenv("FP_ZETA_DISABLE", "1")
    plugin = ZetaPlugin()
    reg = FakeRegistry()
    ctx = FakeCtx()
    asyncio.run(plugin._on_init(ctx, tool_registry=reg))
    assert reg.tools == {}
    assert "system_prompt_append" not in ctx.data


def test_enabled_registers_tools_and_starts(monkeypatch, tmp_path):
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(tmp_path / "peers"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)

    plugin = ZetaPlugin()
    reg = FakeRegistry()
    ctx = FakeCtx()

    async def _run():
        await plugin._on_init(ctx, tool_registry=reg)
        assert "没有在线邻居" in await reg.tools["peer_list"]({})
        plugin._stop()
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert set(reg.tools) == {"peer_list", "peer_send", "peer_answer", "peer_propose", "peer_ack"}
    assert ZETA_DESCRIPTION in ctx.data["system_prompt_append"]


def test_lock_conflict_degrades_visibly(monkeypatch, tmp_path):
    """同名实例（同一 sid）真冲突：第二个丧失邻居面，但对 LLM 可见（工具仍在、返回原因）。

    §1.5 修订后：默认多实例，仅**同名**（同 sid）冲突——不同 sid 的实例可同 workspace 共存。
    注意"真冲突"= 占用者是**别的活进程**；同进程同名（reload）是接管，见下一用例。
    """
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("FP_ZETA_STATE_DIR", str(tmp_path / "zeta_state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)

    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
    try:
        _plant_holder(ws, "s_same", pid=proc.pid)
        second = ZetaPlugin()
        reg2 = FakeRegistry()
        ctx2 = FakeCtx()

        async def _run():
            await second._on_init(ctx2, tool_registry=reg2, state=FakeState("s_same"))
            # 工具照常注册（LLM 能看到"邻居能力存在"），但调用返回降级原因
            msg = await reg2.tools["peer_list"]({})
            assert "实例登记冲突" in msg
            second._stop()
            await asyncio.sleep(0)

        asyncio.run(_run())
    finally:
        proc.kill()
        proc.wait()

    assert set(reg2.tools) == {"peer_list", "peer_send", "peer_answer", "peer_propose", "peer_ack"}
    assert any("不可用" in s for s in ctx2.data["system_prompt_append"])


def test_reload_reattaches_instead_of_degrading(monkeypatch, tmp_path):
    """reload 回归：同进程同 sid 重建插件（POSIX execve 后 PID 不变）→ 接管，不得丧失邻居面。

    旧实例不会跑 shutdown 钩子（进程原地重生），登记必然残留；若把"我自己刚才那条"
    当成别人占用，新实例就永久降级——这正是本 bug 的表现。
    """
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("FP_ZETA_STATE_DIR", str(tmp_path / "zeta_state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)

    old = ZetaPlugin()  # 旧化身：登记已落盘，随后被 execve 抹掉（不会注销）
    new = ZetaPlugin()  # 新化身：同 sid → 同名
    reg_new = FakeRegistry()
    ctx_new = FakeCtx()

    async def _run():
        await old._on_init(FakeCtx(), tool_registry=FakeRegistry(), state=FakeState("s_same"))
        await new._on_init(ctx_new, tool_registry=reg_new, state=FakeState("s_same"))
        msg = await reg_new.tools["peer_list"]({})
        assert "邻居表" in msg, msg
        assert "不可用" not in msg
        old._stop()
        new._stop()
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert not any("不可用" in s for s in ctx_new.data["system_prompt_append"])


def test_same_workspace_different_sid_coexist(monkeypatch, tmp_path):
    """B1：同 workspace、不同 sid 的两个实例都能启用邻居面（记忆共享前提）。"""
    monkeypatch.delenv("FP_ZETA_DISABLE", raising=False)
    monkeypatch.setenv("FP_ZETA_PEERS_DIR", str(tmp_path / "peers"))
    monkeypatch.setenv("FP_ZETA_STATE_DIR", str(tmp_path / "zeta_state"))
    ws = tmp_path / "ws"
    ws.mkdir()
    monkeypatch.chdir(ws)

    a, b = ZetaPlugin(), ZetaPlugin()
    reg_b = FakeRegistry()
    ctx_b = FakeCtx()

    async def _run():
        await a._on_init(FakeCtx(), tool_registry=FakeRegistry(), state=FakeState("s_aaa"))
        await b._on_init(ctx_b, tool_registry=reg_b, state=FakeState("s_bbb"))
        # 两个都在线：b 的 peer_list 应看到 a
        assert "ws-" in await reg_b.tools["peer_list"]({})
        a._stop()
        b._stop()
        await asyncio.sleep(0)

    asyncio.run(_run())

    assert not any("不可用" in s for s in ctx_b.data["system_prompt_append"])
