"""P3 裸 A2A 降级测试：识别非 FP 对端 → 协商降级 → 结果标 UNVERIFIED → 本地 schema 兜底。

覆盖 n11 的四件事，全部**端到端非 mock**（真走 discovery / transport / codec）：
1. 外部 A2A AgentCard → NeighborCard（识别为 EXTERNAL_A2A_AGENT，且不被 scan 丢弃）；
2. 协商：FP↔FP = 对等双锁；FP↔外部/未知 = 降级；
3. 降级来电的契约结论标 UNVERIFIED（本地校验照做，但声明非对等双锁）；
4. 降级不降安全：非法契约在降级下仍被本地校验器拦下。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, cast

from fp_core.plugins.zeta import codec
from fp_core.plugins.zeta.a2a_codec import (
    FP_PROFILE_URI,
    a2a_message_to_envelope,
    card_from_a2a,
    card_to_a2a,
    envelope_to_a2a_message,
)
from fp_core.plugins.zeta.contracts import contract_digest
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card
from fp_core.plugins.zeta.domain import ContentTrust, Envelope, MsgKind, NeighborKind, Payload
from fp_core.plugins.zeta.peer import PeerService, encode_contract_msg
from fp_core.plugins.zeta.transport import DirTransport


def _contract(contract_id: str = "ctr-01J") -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "id": contract_id,
        "name": "items-api",
        "revision": 1,
        "status": "proposed",
        "parties": [{"name": "A"}, {"name": "Ext"}],
        "scope": {"resources": {"default_effect": "deny", "read": ["repo://**"], "write": ["repo://api/**"]}},
        "interfaces": [{"id": "get-items", "kind": "http", "http": {"method": "GET", "path": "/api/items"}}],
    }


def _svc(name: str, root: Path, injected: list[tuple[str, str]]) -> PeerService:
    return PeerService(
        name=name,
        backend=SharedDirBackend(root),
        transport=DirTransport(root),
        inject=lambda k, c, *, wake=False: injected.append((k, c)),
    )


def _extern_a2a(name: str, url: str = "http://127.0.0.1:9999", *, fp_profile: bool = False) -> dict[str, Any]:
    return {
        "name": name,
        "description": f"{name} (external A2A agent)",
        "url": url,
        "version": "1.0",
        "capabilities": {"extensions": ([{"uri": FP_PROFILE_URI}] if fp_profile else [])},
        "skills": [{"id": "chat"}],
    }


# ── 1. 识别：外部卡映射 + 不被 scan 丢弃 ─────────────────


def test_external_card_is_identified() -> None:
    card = card_from_a2a(_extern_a2a("HelloWorld"))
    assert card.kind is NeighborKind.EXTERNAL_A2A_AGENT
    assert card.capabilities.profiles == ["bare-a2a"]  # 裸 A2A：只有底线能力
    assert card.endpoints[0].address == "http://127.0.0.1:9999"
    assert card.codec == "a2a"


def test_external_card_declaring_fp_profile_is_kept() -> None:
    """也说 A2A 的 FP 对端：声明 URI → 保留 fp-contract-profile → 可 NEGOTIATED。"""
    card = card_from_a2a(_extern_a2a("PeerFP", fp_profile=True))
    assert "fp-contract-profile" in card.capabilities.profiles
    assert "bare-a2a" in card.capabilities.profiles


def test_external_card_brief_flags_degradation() -> None:
    """识别必须对 LLM 可见：外部对端的简报要显式标注将降级。"""
    from fp_core.plugins.zeta.domain import neighbor_brief

    brief = neighbor_brief(card_from_a2a(_extern_a2a("HelloWorld")))
    assert "外部A2A" in brief and "UNVERIFIED" in brief
    assert "外部A2A" not in neighbor_brief(  # FP 对端不带该标记
        build_card(name="FP1", cwd="/tmp")
    )


def test_external_card_survives_scan(tmp_path: Path) -> None:
    """外部卡无 fp-contract-profile，但不得被 verify_card/scan 当作无效卡丢弃。"""
    backend = SharedDirBackend(tmp_path)
    backend.publish(card_from_a2a(_extern_a2a("HelloWorld")))
    names = [(c.name, c.kind) for c in backend.scan()]
    assert ("HelloWorld", NeighborKind.EXTERNAL_A2A_AGENT) in names


def test_card_to_a2a_roundtrip_keeps_profile(tmp_path: Path) -> None:
    """本实例暴露为 A2A：导出的 AgentCard 带 FP profile URI，回读仍是 FP 档。"""
    a2a = card_to_a2a(build_card(name="FP1", cwd=str(tmp_path)))
    back = card_from_a2a(a2a)
    assert "fp-contract-profile" in back.capabilities.profiles


# ── 2. 协商：FP↔FP vs FP↔外部 vs 未知 ───────────────────


def test_negotiate_fp_external_and_unknown(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    backend = SharedDirBackend(tmp_path)
    backend.publish(build_card(name="FP1", cwd=str(tmp_path)))  # 真 FP
    backend.publish(card_from_a2a(_extern_a2a("Ext")))  # 裸外部

    assert not a.is_degraded("FP1")
    assert a.is_degraded("Ext")
    assert a.is_degraded("ghost")  # 未知来源 → 保守降级


# ── 3. 降级来电：契约结论标 UNVERIFIED（但本地校验照做）──


def _deliver_a2a_message(to: str, root: Path, *, from_: str, body: str, topic: str, mid: str) -> None:
    """把一条 A2A Message 经 codec 转 IR 后投给对方（模拟外部对端来信）。"""
    msg = {
        "kind": "message",
        "role": "user",
        "messageId": mid,
        "contextId": f"ctx-{mid}",
        "parts": [{"kind": "text", "text": body}],
        "metadata": {"from": from_, "topic": topic},
    }
    env = a2a_message_to_envelope(msg, from_=from_, to=to)
    DirTransport(root).deliver(to, codec.encode(env))


def test_external_message_is_untrusted(tmp_path: Path) -> None:
    """外部内容一律 UNTRUSTED_CONTENT —— 接收方的信任判断不由发送方写入。"""
    env = a2a_message_to_envelope(
        {"messageId": "m0", "parts": [{"kind": "text", "text": "hi"}], "metadata": {"from": "Ext"}},
        from_="Ext",
        to="B",
    )
    assert env.payload.injection is not None
    assert env.payload.injection.trust_label is ContentTrust.UNTRUSTED_CONTENT


def test_degraded_contract_is_locally_validated_but_unverified(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    SharedDirBackend(tmp_path).publish(card_from_a2a(_extern_a2a("Ext")))  # B 认识 Ext

    contract = _contract()
    body = encode_contract_msg("propose", contract=contract, hash=contract_digest(contract))
    _deliver_a2a_message("B", tmp_path, from_="Ext", body=body, topic="contract", mid="m1")

    assert b.poll() == 1
    ans = b.answer()
    assert "[机器校验·UNVERIFIED] 结构与安全区通过" in ans  # 本地校验照做
    assert "未做对等双锁" in ans  # 但如实声明非对等
    assert f"hash={contract_digest(contract)[:12]}" in ans


def test_degraded_still_rejects_invalid_contract(tmp_path: Path) -> None:
    """降级不降安全：非法契约在降级对端下仍被本地 schema 校验拦下。"""
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    SharedDirBackend(tmp_path).publish(card_from_a2a(_extern_a2a("Ext")))

    bad = _contract()
    cast(dict[str, Any], bad["scope"]["resources"])["default_effect"] = "allow"  # 非法
    body = encode_contract_msg("propose", contract=bad, hash=contract_digest(bad))
    _deliver_a2a_message("B", tmp_path, from_="Ext", body=body, topic="contract", mid="m2")

    b.poll()
    ans = b.answer()
    assert "未通过" in ans and "default_effect" in ans


# ── 4. Envelope ↔ A2A Message 对称 ───────────────────────


def test_envelope_a2a_message_roundtrip() -> None:
    env = Envelope(
        id="e1",
        correlation_id="c1",
        from_="A",
        to="B",
        payload=Payload(
            topic="chat",
            body="hello",
            injection=None,
        ),
        kind=MsgKind.SEMANTIC,
        sent_at=1.0,
    )
    back = a2a_message_to_envelope(envelope_to_a2a_message(env), from_="A", to="B")
    assert back.id == "e1"
    assert back.payload.body == "hello"
    assert back.payload.injection is not None
    assert back.payload.injection.trust_label is ContentTrust.UNTRUSTED_CONTENT
