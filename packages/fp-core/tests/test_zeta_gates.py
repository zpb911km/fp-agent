"""P1 闸门测试：入口处的形状上限（字节 / 跳数 / 信任档）。

为什么这些"看起来只是限流"的东西属于安全面
==========================================
三条闸门都在**解码之后、注入之前**，但真正的成本分界在解码之前/之后：

- **字节上限**必须在解码**前**判（本实现如此）。否则"攻击面就是解码器本身"：
  一个 50 MiB 的 JSON 会先把内存和 CPU 花掉，才轮到你说"我不要"。
- **跳数上限**保护的是**将来**：本实例永不转发（MVP 单跳），但一旦接上路由器，
  任何可被第三方触发的转发都是放大器。现在就把 ``hops >= max_hops`` 钉成拒收，
  是为了让"加路由器"这件事无法在不知不觉中把邻居面变成 DDoS 反射面。
- **信任档闸门**是 P0 归属的落地点：归属判定的结论只有接到"允许什么主题"上才有
  意义，否则验了也白验。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from zeta_signing import make_svc, publish_self, sign_env, state_dir

from fp_core.plugins.zeta import audit, codec
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card
from fp_core.plugins.zeta.domain import Envelope, MsgKind, NeighborKind, Payload, Ttl
from fp_core.plugins.zeta.peer import MAX_ENVELOPE_BYTES, PeerService
from fp_core.plugins.zeta.transport import DirTransport


def _svc(root: Path, injected: list[tuple[str, str]], **kw: Any) -> PeerService:
    return make_svc("B", root, lambda k, c, *, wake=False: injected.append((k, c)), **kw)


def _events(root: Path) -> list[str]:
    return [str(e.get("event")) for e in audit.tail(80, root=state_dir(root))]


def _env(body: str, *, from_: str = "A", topic: str = "chat", msg_id: str = "m1", hops: int = 0, **kw: Any) -> Envelope:
    return Envelope(
        id=msg_id,
        correlation_id="c1",
        from_=from_,
        to="B",
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic=topic, body=body),
        hops=hops,
        sent_at=1.0,
        **kw,
    )


def _deliver(root: Path, env: Envelope, *, sign: str | None = "A") -> None:
    if sign is not None:
        publish_self(sign, root)
        sign_env(env, sign, root)
    DirTransport(root).deliver("B", codec.encode(env))


# ── 字节上限 ────────────────────────────────────────────


def test_oversized_envelope_rejected_before_decode(tmp_path: Path) -> None:
    """超限信封**不解 JSON** 直接拒：解码本身就是攻击面。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected, max_envelope_bytes=2048, untrusted_max_body=1 << 20)

    _deliver(tmp_path, _env("z" * 8000), sign=None)
    assert b.poll() == 0
    assert "poll.reject.envelope_too_large" in _events(tmp_path)


def test_oversized_rejection_happens_even_if_payload_would_be_valid(tmp_path: Path) -> None:
    """合法来源的超大信同样被拒：上限是形状约束，不因"是熟人"而放宽。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected, max_envelope_bytes=2048, untrusted_max_body=1 << 20)

    _deliver(tmp_path, _env("z" * 8000))  # 已签名
    assert b.poll() == 0
    assert "poll.reject.envelope_too_large" in _events(tmp_path)


def test_default_limit_is_a_sane_bound(tmp_path: Path) -> None:
    """默认上限存在且有界（防止"默认不限"这种静默退化）。"""
    assert 4 << 10 <= MAX_ENVELOPE_BYTES <= 1 << 20


# ── 跳数 ────────────────────────────────────────────────


def test_forwarded_envelope_is_rejected(tmp_path: Path) -> None:
    """``hops >= max_hops`` 即拒：本实例永不转发，转发来的信不是寄给我的。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected)

    _deliver(tmp_path, _env("别人转寄的", hops=1))
    assert b.poll() == 0
    assert "poll.reject.hop_limit" in _events(tmp_path)


def test_hop_limit_comes_from_envelope_ttl_bound(tmp_path: Path) -> None:
    """跳数上限随 ``ttl.max_hops`` 走（信自己声明能走几跳）。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected)

    env = _env("允许两跳", topic="chat", hops=1, ttl=Ttl(max_hops=2))
    _deliver(tmp_path, env)
    assert b.poll() == 1  # 1 < 2：放行

    env2 = _env("已被转两次", topic="chat", msg_id="m2", hops=2, ttl=Ttl(max_hops=2))
    _deliver(tmp_path, env2)
    assert b.poll() == 0


def test_we_never_forward_and_never_raise_hops(tmp_path: Path) -> None:
    """本实例发出的信 ``hops == 0``——我们不自称转发，也不替别人转发（不自举放大）。"""
    injected: list[tuple[str, str]] = []
    a = make_svc("A", tmp_path, lambda k, c, *, wake=False: injected.append((k, c)))
    publish_self("B", tmp_path, business="后端")

    a.send("B", "你好", topic="chat")
    raw = list((tmp_path / "B.inbox").glob("msg-*.json"))
    assert raw, "信没投出去"
    env = codec.decode(raw[0].read_text(encoding="utf-8"))
    assert env.hops == 0


# ── 信任档闸门 ──────────────────────────────────────────


def test_untrusted_source_only_allowed_on_chat(tmp_path: Path) -> None:
    """未验证来源只许 ``chat``：contract 类要机器校验，不能由不可归属者发起。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected)

    for topic in ("contract", "task", "control"):
        _deliver(tmp_path, _env("请求", topic=topic, msg_id=f"m-{topic}"), sign=None)
    assert b.poll() == 0
    rejected = [e for e in _events(tmp_path) if e == "poll.reject.untrusted_topic"]
    assert len(rejected) == 3


def test_external_a2a_peer_is_allowed_but_marked_unverified(tmp_path: Path) -> None:
    """外部异构对端是**设计中的互操作**：放行，但报告与接听一律标 UNVERIFIED。

    这条例外的存在理由必须写清楚：外部 A2A agent 根本不会说我们的签名语言。给它
    留一条路，是"可互操作"；把它的产物标成 UNVERIFIED，是"不信任"。两者不能混。
    """
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected)
    # 外部对端：名片声明 kind=EXTERNAL_A2A_AGENT，且没有 identity（不签我们的名）
    SharedDirBackend(tmp_path).publish(
        build_card(name="X", cwd=str(tmp_path), business="external", kind=NeighborKind.EXTERNAL_A2A_AGENT)
    )

    _deliver(tmp_path, _env("外部系统的请求", from_="X", topic="contract"), sign=None)
    assert b.poll() == 1
    answer = b.answer()
    assert "UNVERIFIED" in answer
    assert "外部/降级对端" in answer


def test_mechanical_messages_are_never_injected(tmp_path: Path) -> None:
    """红线 #2：机械消息永不注入（心跳/状态同步不进上下文，无论谁发的）。"""
    injected: list[tuple[str, str]] = []
    b = _svc(tmp_path, injected)

    env = _env("heartbeat", topic="chat")
    env.kind = MsgKind.MECHANICAL
    _deliver(tmp_path, env)
    assert b.poll() == 0
