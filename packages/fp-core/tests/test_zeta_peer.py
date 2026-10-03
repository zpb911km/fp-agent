"""Zeta codec / 传输 / 对等服务测试（P1b）。"""

from __future__ import annotations

import time
from pathlib import Path

from fp_core.plugins.zeta import codec
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card
from fp_core.plugins.zeta.domain import (
    Envelope,
    InjectAs,
    InjectionHint,
    MsgKind,
    Payload,
    Ttl,
)
from fp_core.plugins.zeta.peer import PeerService
from fp_core.plugins.zeta.transport import DirTransport


def _svc(name: str, root: Path, injected: list[tuple[str, str]]) -> PeerService:
    return PeerService(
        name=name,
        backend=SharedDirBackend(root),
        transport=DirTransport(root),
        inject=lambda k, c, *, wake=False: injected.append((k, c)),
    )


# ── codec 往返 ──────────────────────────────────────────


def test_codec_roundtrip():
    env = Envelope(
        id="e1",
        correlation_id="c1",
        from_="A",
        to="B",
        kind=MsgKind.SEMANTIC,
        payload=Payload(
            topic="contract",
            body="约定 GET /items",
            injection=InjectionHint(inject_as=InjectAs.USER_MESSAGE),
        ),
        sent_at=123.0,
    )
    back = codec.decode(codec.encode(env))
    assert back.id == "e1" and back.from_ == "A" and back.to == "B"
    assert back.payload.topic == "contract" and back.payload.body == "约定 GET /items"
    assert back.should_inject()


# ── 发送 → 轮询 → 铃声 → 接听 ───────────────────────────


def test_send_then_poll_rings_and_answer(tmp_path: Path):
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)

    # B 发布名片（A 才能判定 B 在线）
    SharedDirBackend(tmp_path).publish(build_card(name="B", cwd=str(tmp_path), business="后端"))

    msg_id, online = a.send("B", "一起定 API 契约", topic="contract")
    assert msg_id and online is True

    assert b.poll() == 1  # B 收到一条语义消息
    assert injected and injected[0][0] == "peer_ring"
    assert "peer:A" in injected[0][1]
    assert "一起定 API 契约" not in injected[0][1], "铃声不含正文（来电显示级）"

    answer = b.answer()
    assert "一起定 API 契约" in answer and "peer:A" in answer
    assert b.pending() == []


def test_ring_injection_is_wake_event(tmp_path: Path):
    """铃声注入带 wake=True —— 空闲实例应被唤醒一轮（core 空闲泵 / portal.run.wake）。"""
    rings: list[tuple[str, str, bool]] = []
    b = PeerService(
        name="B",
        backend=SharedDirBackend(tmp_path),
        transport=DirTransport(tmp_path),
        inject=lambda k, c, *, wake=False: rings.append((k, c, wake)),
    )
    SharedDirBackend(tmp_path).publish(build_card(name="B", cwd=str(tmp_path), business="后端"))
    a = _svc("A", tmp_path, [])

    a.send("B", "在吗？", topic="chat")
    assert b.poll() == 1
    assert rings and rings[0][0] == "peer_ring"
    assert rings[0][2] is True, "铃声必须是唤醒级事件（空闲实例据此被唤醒）"


def test_offline_leave_message(tmp_path: Path):
    """对方离线：消息仍投（留话），上线后能收到——邻居搬家无所谓。"""
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)

    _msg_id, online = a.send("B", "在吗？", topic="chat")
    assert online is False, "B 未发布名片 → 判定离线"
    assert b.poll() == 1, "B 上线后仍能读到离线留话"


def test_mechanical_never_injected(tmp_path: Path):
    """机械消息由网关应答，永不进 LLM（红线 #2）。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    mech = Envelope(
        id="m1",
        correlation_id="c1",
        from_="A",
        to="B",
        kind=MsgKind.MECHANICAL,
        payload=Payload(kind_mech="profile_probe"),
    )
    DirTransport(tmp_path).deliver("B", codec.encode(mech))
    assert b.poll() == 0
    assert injected == []


def test_poll_consumes_once(tmp_path: Path):
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)
    a.send("B", "hi")
    assert b.poll() == 1
    assert b.poll() == 0  # 已消费，不重复


def test_ring_lands_in_real_injection_queue(tmp_path: Path):
    """铃声真的进入 core 环顶注入队列（jobs.inject_event → drain_ready），非 mock。"""
    from fp_core.core import jobs

    a = _svc("A", tmp_path, [])
    b = PeerService(
        name="B",
        backend=SharedDirBackend(tmp_path),
        transport=DirTransport(tmp_path),
        inject=jobs.inject_event,  # 真实注入通道
    )
    jobs.drain_ready()  # 清空队列
    a.send("B", "在吗", topic="chat")
    assert b.poll() == 1
    drained = jobs.drain_ready()
    assert any(m["kind"] == "peer_ring" for m in drained), f"未进入注入队列: {drained}"


# ── 幂等去重 + TTL（§5 离线 / 重复）─────────────────────


def _env(msg_id: str, *, key: str | None = None, ttl: Ttl | None = None) -> Envelope:
    return Envelope(
        id=msg_id,
        correlation_id="c1",
        from_="A",
        to="B",
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic="chat", body=f"来自 {msg_id}"),
        idempotency_key=key,
        ttl=ttl if ttl is not None else Ttl(),
        sent_at=1.0,
    )


def test_duplicate_idempotency_key_rings_once(tmp_path: Path):
    """同一 idempotency_key 重投：at-least-once 传输不产生重复响铃。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    t = DirTransport(tmp_path)
    wire = codec.encode(_env("e1", key="k1"))
    t.deliver("B", wire)
    t.deliver("B", wire)  # 重投：文件名不同，逻辑消息同一
    assert b.poll() == 1, "同 key 重投应只响一次"
    assert len(injected) == 1


def test_duplicate_envelope_id_rings_once(tmp_path: Path):
    """无 idempotency_key 时以信封 id 为幂等键：同一封信重放只处理一次。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    t = DirTransport(tmp_path)
    wire = codec.encode(_env("same-id"))
    t.deliver("B", wire)
    t.deliver("B", wire)
    assert b.poll() == 1


def test_distinct_messages_both_ring(tmp_path: Path):
    """不同消息（不同 id）各自处理——幂等不误合并。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    t = DirTransport(tmp_path)
    t.deliver("B", codec.encode(_env("e1")))
    t.deliver("B", codec.encode(_env("e2")))
    assert b.poll() == 2


def test_expired_message_dropped(tmp_path: Path):
    """过期留话：收到但不处理（不响铃）。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    env = _env("old", ttl=Ttl(deadline=time.time() - 1.0))
    DirTransport(tmp_path).deliver("B", codec.encode(env))
    assert b.poll() == 0
    assert injected == []


def test_send_sets_default_ttl(tmp_path: Path):
    """send 的语义消息带默认存活期，且当前未过期。"""
    a = _svc("A", tmp_path, [])
    SharedDirBackend(tmp_path).publish(build_card(name="B", cwd=str(tmp_path), business="b"))
    a.send("B", "hi")
    env = codec.decode(DirTransport(tmp_path).drain("B")[0])
    assert env.ttl.deadline is not None
    assert not env.ttl.expired()


def test_purge_processed(tmp_path: Path):
    """过期的 .processed 历史文件被回收（存储卫生）。"""
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)
    a.send("B", "hi")
    b.poll()  # 消费 → 移入 .processed
    t = DirTransport(tmp_path)
    done = t.inbox_dir("B") / ".processed"
    files = list(done.glob("msg-*.json"))
    assert files, "应有一条历史文件"
    assert t.purge_processed("B", max_age=0.0, now=time.time() + 1) == len(files)


def test_housekeeping_throttled(tmp_path: Path):
    """维护节流：首次执行、短时间内第二次跳过。"""
    a = _svc("A", tmp_path, [])
    assert a.housekeeping(now=1000.0) == 0  # 首次：无历史可清
    assert a.housekeeping(now=1001.0) == 0  # 节流窗口内不重复扫盘
