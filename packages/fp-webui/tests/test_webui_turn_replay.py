"""本轮重放（turn replay）：agent 忙时新开页 / resync 重连必须能看见本轮内容。

## 被锁住的缺陷（用户实测原话）

    「agent 忙时重连 webui，会卡在一条消息上消息列表不流式刷新，无法看到最新消息，
      刷新网页也没用，只有等 agent 闲时刷新网页，会话才完整。」

根因不在前端渲染，而在**本轮内容有两个都不给它**的出口：

  · REST ``/api/sessions/{sid}/messages`` 读的是**落盘**的会话文件，而会话文件
    在轮次结束才写（core 侧 turn 级落盘）→ 轮次进行中拉历史，只能拿到**上一轮**
    及更早的内容，本轮提问/正文/工具行一行都没有；
  · EventBus 重放只覆盖 ``seq > 客户端 lastSeq``，而重开页面的 lastSeq = 0、
    且 seq=0 时服务端根本不走重放分支（无协商对象）→ 桥上那批事件没人来取。

于是刷新后页面「卡在一条消息上」——那条就是上一轮的旧消息，本轮永远补不进来；
只有等 agent 空闲（会话落盘）+ 再刷一次，历史才完整。

修法：SessionRuntime 在 ``start()`` 记下本轮起点 ``turn_base_seq``，
新连（seq=0）或 resync 时若本轮仍在跑，就把 ``(turn_base_seq, 现在]`` 的全部
事件补发，并用 ``connected.replay_base`` 告知前端本地序号该退到哪。

## 本测试的边界（为什么不连真实 LLM）

用**假 transport + 假 portal**直接驱动 ``websocket_chat`` 协程：
  · 不起服务器、不发网络请求、不碰真实模型 API（用户明确要求）；
  · 但走的是**真** ``websocket_chat`` 代码路径（真 SessionRuntime / 真 EventBus /
    真 WebSocketIO），因此重放协商、序号对齐这些被测点都是生产代码；
  · 假 portal.run.send 只做一件事：按真实事件时序往总线上发
    user_message → llm_start → chunk…，然后**挂住**（模拟 agent 忙）。
"""

from __future__ import annotations

import asyncio
import contextlib
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.websockets import WebSocketDisconnect

from fp_core.api import EventBus
from fp_webui import main

# ── 假 transport ──────────────────────────────────────────

_DISCONNECT = object()


class _FakeWS:
    """websocket_chat 用到的最小接口：accept / send_json / receive_text / close / client"""

    def __init__(self) -> None:
        self.sent: list[dict[str, Any]] = []
        self.inbound: asyncio.Queue[Any] = asyncio.Queue()
        self.client = SimpleNamespace(host="127.0.0.1")
        self.closed_code: int | None = None

    async def accept(self) -> None:
        return None

    async def send_json(self, data: Any) -> None:
        self.sent.append(data)

    async def receive_text(self) -> str:
        item = await self.inbound.get()
        if item is _DISCONNECT:
            raise WebSocketDisconnect(1000)
        return item  # type: ignore[return-value]

    async def close(self, code: int = 1000) -> None:
        self.closed_code = code

    def disconnect(self) -> None:
        self.inbound.put_nowait(_DISCONNECT)


class _FakeRun:
    """portal.run 替身：本轮事件按时序发到总线上，然后挂住（agent 忙）"""

    def __init__(self, bus: EventBus) -> None:
        self.bus = bus
        self.transcript: list[dict[str, Any]] = []
        self.status = SimpleNamespace(session_id="sess-replay", cancelled=False, model="fake")
        self.release = asyncio.Event()
        self.sent_chunks = 0

    async def send(self, msg: str, io: Any = None) -> Any:
        # 真实时序：user_message 由 bridge 在 ON_CTX_APPEND(op=user) 时发，
        # 位置在 start() 记录的 turn_base_seq 之后（本测试据此验证起点抓对了）。
        await self.bus.publish({"type": "user_message", "content": msg})
        await self.bus.publish({"type": "llm_start"})
        for i in range(3):
            await self.bus.publish({"type": "chunk", "content": f"t{i}"})
            self.sent_chunks += 1
            await asyncio.sleep(0)
        await self.release.wait()  # ← 卡在这里：agent 忙，本轮还没落盘
        self.transcript = [
            {"role": "user", "content": msg},
            {"role": "assistant", "content": "t0t1t2"},
        ]
        await self.bus.publish({"type": "llm_end", "content": "t0t1t2", "has_tool_calls": False})
        return SimpleNamespace(content="t0t1t2", metadata={})

    def reply(self, *_a: Any, **_k: Any) -> bool:
        # 注意必须是**同步**返回 bool：handler 用 `if session_runtime.feed_reply(...)`
        # 做判据，返回 coroutine 会被当成真值 → 消息被"注入"分支吞掉，本轮根本不启动。
        return False

    async def submit(self, _content: str) -> str:
        return "queued"


@pytest.fixture()
def env(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """隔离的总线 + 会话运行时 + 假 portal（全部只替换，不改生产逻辑）"""
    bus = EventBus()  # 每个用例干净 seq 空间
    runtime = main.SessionRuntime()  # 干净会话级运行时（turn_base_seq 归 None）
    run = _FakeRun(bus)
    fake_portal = SimpleNamespace(
        events=bus,
        is_open=True,
        run=run,
        ctl=SimpleNamespace(open=None),
    )

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "event_bus", bus)
    monkeypatch.setattr(main, "session_runtime", runtime)
    monkeypatch.setattr(main, "portal", fake_portal)
    monkeypatch.setattr(main, "ensure_agent", _noop)
    return SimpleNamespace(bus=bus, runtime=runtime, run=run, portal=fake_portal)


# ── 驱动辅助 ──────────────────────────────────────────────


async def _settle(n: int = 6) -> None:
    """让出若干轮事件循环，等后台 push_task / 处理任务推进"""
    for _ in range(n):
        await asyncio.sleep(0)


async def _connect(env: SimpleNamespace, seq: int = 0, run_id: str = "") -> tuple[_FakeWS, Any]:
    """起一条真实 websocket_chat 协程，返回 (假 socket, 协程 task)"""
    ws = _FakeWS()
    task = asyncio.create_task(main.websocket_chat(ws, token=main._WEBUI_TOKEN, seq=seq, run_id=run_id))
    await _settle()
    return ws, task


def _frame_types(ws: _FakeWS) -> list[str]:
    return [f.get("type", "?") for f in ws.sent]


def _after_connected(ws: _FakeWS) -> list[dict[str, Any]]:
    """connected 之后收到的全部帧（= 重放/本轮重放/实时事件）"""
    assert ws.sent and ws.sent[0]["type"] == "connected", _frame_types(ws)
    return ws.sent[1:]


async def _stop(*pairs: Any) -> None:
    """收尾：断开假 socket 并回收 handler 协程。

    handler 以 ``except WebSocketDisconnect: pass`` 收场（正常返回），
    若先 cancel 则抛 CancelledError——两种都合法，故统一抑制。
    """
    for ws, task in (pairs[i : i + 2] for i in range(0, len(pairs), 2)):
        ws.disconnect()
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError, WebSocketDisconnect):
            await task


async def _start_busy_turn(env: SimpleNamespace) -> tuple[_FakeWS, Any, int]:
    """起一轮并停在「agent 忙」：返回 (ws1, task1, 本轮起点 seq)"""
    ws1, task1 = await _connect(env)  # 首连（seq=0）
    base = env.bus.current_seq  # 本轮之前的最新序号
    ws1.inbound.put_nowait('{"type": "message", "content": "你好"}')
    # 等本轮事件全部发出（3 个 chunk）—— 此时 send() 卡在 release 上
    for _ in range(20):
        await _settle()
        if env.run.sent_chunks == 3 and env.runtime.is_running:
            break
    assert env.runtime.is_running, "本轮应仍在运行（模拟 agent 忙）"
    assert env.run.sent_chunks == 3
    return ws1, task1, base


# ══════════════════════════════════════════════════════════
# 1. 核心缺陷：新开页面（seq=0）在 agent 忙时必须拿到本轮全部内容
# ══════════════════════════════════════════════════════════


async def _case_fresh_page(env: SimpleNamespace) -> None:
    """刷新页面（seq=0）而 agent 正忙 → 服务端整轮重放，页面能重建本轮。

    「修复前」的行为：connected 里没有 replay_base、之后也没有任何帧，
    新页面只能去拉 REST 历史，而本轮尚未落盘 → 本轮内容永远缺失。
    """
    ws1, task1, base = await _start_busy_turn(env)

    ws2, task2 = await _connect(env, seq=0)  # 全新页面，无 seq
    conn = ws2.sent[0]
    assert conn["processing"] is True, "快照必须告知 agent 正忙"
    assert conn["resync"] is False, "seq=0 是首连，不该报 resync"
    assert conn["replay_base"] == base, (
        f"本轮重放起点必须是 turn_base_seq（本轮第一件事之前），实测 {conn.get('replay_base')} != {base}"
    )

    replay = _after_connected(ws2)
    types = [f["type"] for f in replay]
    assert types == ["user_message", "llm_start", "chunk", "chunk", "chunk"], (
        f"本轮至今的事件必须原样重放（提问→思考→已生成正文），实测 {types}"
    )
    assert replay[0]["content"] == "你好"
    assert "".join(f.get("content", "") for f in replay if f["type"] == "chunk") == "t0t1t2"

    # 序号必须严格递增且都 > replay_base：前端凭此逐条推进 lastSeq 不被去重器丢掉
    seqs = [f["seq"] for f in replay]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs)
    assert seqs[0] == base + 1, f"首条重放事件应紧接起点，实测 {seqs[0]} vs base+1={base + 1}"

    # 收尾：放行本轮 → 两条连接都拿到 llm_end / done（重放不影响实时续传）
    env.run.release.set()
    await _settle(12)
    assert "done" in _frame_types(ws2), f"重放连接必须能续上本轮收尾，实测 {_frame_types(ws2)}"

    await _stop(ws1, task1, ws2, task2)


# ══════════════════════════════════════════════════════════
# 2. resync 重连（服务端重启 / 缓冲溢出）：历史里没有本轮，同样靠重放补
# ══════════════════════════════════════════════════════════


async def _case_resync(env: SimpleNamespace) -> None:
    """run_id 变了（服务端重启）→ resync，但本轮仍在跑 → 本轮内容一并补发"""
    ws1, task1, base = await _start_busy_turn(env)

    ws2, task2 = await _connect(env, seq=7, run_id="deadbeef")  # 旧进程的 run_id
    conn = ws2.sent[0]
    assert conn["resync"] is True
    assert conn["replay"] is False, "resync 时不该标 replay（前端据此清屏重拉历史）"
    assert conn["replay_base"] == base

    types = [f["type"] for f in _after_connected(ws2)]
    assert types == ["user_message", "llm_start", "chunk", "chunk", "chunk"], types

    await _stop(ws1, task1, ws2, task2)


# ══════════════════════════════════════════════════════════
# 3. 不误伤：空闲连入 / 正常续传都不该冒 replay_base
# ══════════════════════════════════════════════════════════


async def _case_idle(env: SimpleNamespace) -> None:
    """没有正在跑的轮次 → replay_base 必须为 None，且不额外发任何帧"""
    env.bus._seq = 3  # 模拟「有历史事件、但没在跑」
    ws, task = await _connect(env, seq=0)
    conn = ws.sent[0]
    assert conn["processing"] is False
    assert conn["replay_base"] is None
    assert _after_connected(ws) == [], "空闲连入不该被塞任何重放帧"

    await _stop(ws, task)


async def _case_normal(env: SimpleNamespace) -> None:
    """seq>0 且 run_id 匹配（正常续传）→ 走原有 replay，不叠加本轮重放"""
    ws1, task1, base = await _start_busy_turn(env)
    run_id = env.bus.run_id
    # 客户端自称已消费到 base+2（本轮中段断线）→ 只该补 base+2 之后的事件
    ws2, task2 = await _connect(env, seq=base + 2, run_id=run_id)
    conn = ws2.sent[0]
    assert conn["replay"] is True
    assert conn["resync"] is False
    assert conn["replay_base"] is None, "正常续传不该冒 replay_base（会把 lastSeq 拽回去重放）"
    seqs = [f["seq"] for f in _after_connected(ws2)]
    assert seqs and seqs[0] == base + 3, f"应从 lastSeq 之后续起，实测 {seqs}"

    await _stop(ws1, task1, ws2, task2)


# ══════════════════════════════════════════════════════════
# 4. 诚实降级：本轮起点已被挤出环形缓冲时不装作能重放
# ══════════════════════════════════════════════════════════


async def _case_overflow(env: SimpleNamespace, monkeypatch: pytest.MonkeyPatch) -> None:
    """缓冲溢出（turn_base_seq 之后的事件已被挤出）→ 不做本轮重放。

    拿半截事件重建会得到「没有提问、正文从中间开始」的残缺卡片——比什么都不给
    更容易误导用户以为内容丢了。降级为历史-only 是诚实的选择。
    """
    tiny = EventBus(buffer_size=2)
    monkeypatch.setattr(main, "event_bus", tiny)
    env.bus = tiny
    env.run.bus = tiny

    ws1, task1, _base = await _start_busy_turn(env)
    ws2, task2 = await _connect(env, seq=0)
    conn = ws2.sent[0]
    assert conn["replay_base"] is None, "溢出时必须放弃本轮重放"
    assert _after_connected(ws2) == []

    await _stop(ws1, task1, ws2, task2)


# ── 同步包装（本仓未装 pytest-asyncio，约定 asyncio.run 驱动）──────


def test_fresh_page_gets_whole_turn_while_busy(env):
    """agent 忙时刷新页面（seq=0）→ 服务端整轮重放，本轮提问/正文都回来了"""
    asyncio.run(_case_fresh_page(env))


def test_resync_reconnect_gets_whole_turn(env):
    """resync 重连（服务端重启/缓冲溢出）→ 本轮仍可重放"""
    asyncio.run(_case_resync(env))


def test_idle_connection_has_no_turn_replay(env):
    """空闲连入不发本轮重放（不误伤）"""
    asyncio.run(_case_idle(env))


def test_normal_continuation_keeps_old_semantics(env):
    """正常续传（seq>0 且 run_id 匹配）保持原有 replay 语义"""
    asyncio.run(_case_normal(env))


def test_overflowed_turn_buffer_degrades_honestly(env, monkeypatch):
    """本轮起点被挤出缓冲 → 放弃重放而不是给残缺卡片"""
    asyncio.run(_case_overflow(env, monkeypatch))


# ══════════════════════════════════════════════════════════
# 5. 契约：前端必须按 replay_base 退序号（防「事件被去重器整批丢掉」）
# ══════════════════════════════════════════════════════════


def test_frontend_rebases_lastseq_on_replay_base() -> None:
    """wire.js 的 connected 分支必须把 conn.lastSeq 对齐到 replay_base。

    只发事件不改序号等于白做：本轮重放事件的 seq 全都 ≤ 快照点，
    前端 onmessage 的 `seq <= lastSeq → return` 会把它们整批丢掉。
    这里做源码级契约断言（与 protocol.py 的契约测试同风格）。
    """
    import pathlib

    src = (pathlib.Path(main.__file__).parent / "static" / "js" / "features" / "wire.js").read_text(encoding="utf-8")
    assert "replay_base" in src, "前端未读取 connected.replay_base"
    assert "replayBase !== null ? replayBase" in src, (
        "前端必须在有本轮重放时把 lastSeq 退到 replayBase（否则事件被去重器丢掉）"
    )
