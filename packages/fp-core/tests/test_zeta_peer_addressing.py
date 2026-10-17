"""邻居寻址回归：展示名（``peer:<name>``）不得污染寻址名。

起因（真实故障）：LLM 照抄 ``peer_list`` / 铃声里的展示写法 ``peer:SFG2-738f5d6e``
填进 ``peer_send`` 的 ``to``，于是消息被写进 ``<root>/peerSFG2-738f5d6e.inbox``，
而对方轮询读的是 ``<root>/SFG2-738f5d6e.inbox`` —— 落盘成功、回执报「已留话」，
却永远不会响铃（静默丢件）。本文件把「展示/寻址两套写法必须归一」钉成断言。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from fp_core.plugins.zeta.discovery import SharedDirBackend
from fp_core.plugins.zeta.domain import normalize_peer_name
from fp_core.plugins.zeta.peer import PeerService
from fp_core.plugins.zeta.transport import DirTransport


def _svc(name: str, root: Path, injected: list[tuple[str, str]]) -> PeerService:
    return PeerService(
        name=name,
        backend=SharedDirBackend(root),
        transport=DirTransport(root),
        inject=lambda k, c, *, wake=False: injected.append((k, c)),
    )


# ── 归一化本身 ──────────────────────────────────────────


@pytest.mark.parametrize(
    ("raw", "want"),
    [
        ("B", "B"),
        ("  B  ", "B"),
        ("peer:B", "B"),
        ("peer: B", "B"),
        ("PEER:B", "B"),  # 大小写不敏感
        ("peer：B", "B"),  # 全角冒号
        ("peer:peer:B", "B"),  # 双前缀（旧版本回执会拼出这种）
        ("`peer:B`", "B"),  # LLM 附带的反引号/引号
        ('"peer: B"', "B"),
        ("<peer:B>", "B"),
        ("【peer:B】", "B"),
        ("", ""),
        ("   ", ""),
    ],
)
def test_normalize_peer_name(raw: str, want: str) -> None:
    assert normalize_peer_name(raw) == want


def test_normalize_keeps_name_that_merely_contains_peer() -> None:
    # 只剥**前缀**；名字里的 peer 字样不动（避免把 peeragent-xxx 剥残）
    assert normalize_peer_name("peeragent-7394e83c") == "peeragent-7394e83c"
    assert normalize_peer_name("mypeer:B") == "mypeer:B"


# ── 传输层：展示写法必须落到同一收件箱 ──────────────────


def test_display_name_and_bare_name_share_one_inbox(tmp_path: Path) -> None:
    t = DirTransport(tmp_path)
    assert t.inbox_dir("peer:B") == t.inbox_dir("B") == tmp_path / "B.inbox"

    t.deliver("peer:B", '{"x": 1}')
    assert t.has_pending("B"), "带前缀投递的消息必须能被裸名轮询看到"
    assert len(t.drain("B")) == 1
    assert not (tmp_path / "peerB.inbox").exists(), "不得再产生 peerB.inbox 这类错位目录"


def test_answer_carries_normalized_to(tmp_path: Path) -> None:
    from fp_core.plugins.zeta import codec
    from fp_core.plugins.zeta.domain import Envelope, MsgKind, Payload

    t = DirTransport(tmp_path)
    env = Envelope(
        id="e1",
        correlation_id="c1",
        from_="A",
        to="B",
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic="chat", body="hi"),
        sent_at=1.0,
    )
    t.deliver("peer:B", codec.encode(env))
    assert codec.decode(t.drain("B")[0]).to == "B"  # 信封不掺前缀（寻址同源）


# ── 服务层：带前缀发也能响铃 ────────────────────────────


def test_send_with_display_prefix_rings_the_receiver(tmp_path: Path) -> None:
    from fp_core.plugins.zeta import codec

    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)

    _ = a.send("peer:B", "在吗", topic="chat")
    files = list((tmp_path / "B.inbox").glob("msg-*.json"))
    assert len(files) == 1
    assert codec.decode(files[0].read_text(encoding="utf-8")).to == "B", "信封 to 必须是寻址名"

    assert b.poll() == 1, "带前缀投递的消息必须能被接收方轮询到"
    assert injected and injected[0][0] == "peer_ring"
    assert "在吗" in b.answer()


def test_send_with_display_prefix_keeps_online_detection(tmp_path: Path) -> None:
    """在线判断也走归一化：带前缀的 to 必须仍能认出在线邻居。"""
    from fp_core.plugins.zeta.discovery import build_card

    SharedDirBackend(tmp_path).publish(build_card(name="B", cwd=str(tmp_path), business="后端"))
    a = _svc("A", tmp_path, [])

    assert a.find("peer:B") is not None, "带前缀的名字必须能查到邻居卡"
    _, online = a.send("peer:B", "hi")
    assert online is True
    assert (tmp_path / "B.inbox").exists()
