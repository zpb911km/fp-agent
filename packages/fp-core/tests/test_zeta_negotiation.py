"""P2 契约协商测试：propose → 机器校验 → ack（端到端，非 mock）。"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fp_core.plugins.zeta import codec
from fp_core.plugins.zeta.contracts import ContractExchange, contract_digest
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card
from fp_core.plugins.zeta.domain import Envelope, MsgKind, Payload
from fp_core.plugins.zeta.peer import PeerService, encode_contract_msg
from fp_core.plugins.zeta.transport import DirTransport


def _contract(contract_id: str = "ctr-01J") -> dict[str, Any]:
    return {
        "contract_version": "1.0",
        "id": contract_id,
        "name": "items-api",
        "revision": 1,
        "status": "proposed",
        "parties": [
            {"name": "A", "role": "backend"},
            {"name": "B", "role": "frontend"},
        ],
        "scope": {
            "resources": {
                "default_effect": "deny",
                "read": ["repo://server/src/**"],
                "write": ["repo://server/api/**"],
            }
        },
        "interfaces": [
            {"id": "get-items", "kind": "http", "http": {"method": "GET", "path": "/api/items"}},
        ],
    }


def _svc(
    name: str,
    root: Path,
    injected: list[tuple[str, str]],
    exchange: ContractExchange | None = None,
) -> PeerService:
    return PeerService(
        name=name,
        backend=SharedDirBackend(root),
        transport=DirTransport(root),
        inject=lambda k, c, *, wake=False: injected.append((k, c)),
        exchange=exchange,
    )


def _online(root: Path, *names: str) -> None:
    backend = SharedDirBackend(root)
    for nm in names:
        backend.publish(build_card(name=nm, cwd=str(root), business="test"))


# ── propose：本地自检 → 投递 → 对方机器校验 ──────────────


def test_propose_then_answer_carries_machine_report(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    a, b = _svc("A", tmp_path, injected), _svc("B", tmp_path, injected)
    _online(tmp_path, "A", "B")  # 双方都在邻居表 → FP↔FP = 对等双锁（VERIFIED）

    contract = _contract()
    _msg_id, online, issues = a.propose_contract("B", contract)
    assert issues == [] and online is True

    assert b.poll() == 1
    assert "[铃声]" in injected[0][1] or "peer:A" in injected[0][1]
    answer = b.answer()
    assert "[机器校验] 结构与安全区通过" in answer
    assert f"hash={contract_digest(contract)[:12]}" in answer


def test_propose_invalid_contract_is_flagged_by_both_sides(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    a, b = _svc("A", tmp_path, injected), _svc("B", tmp_path, injected)
    _online(tmp_path, "B")

    bad = _contract()
    bad["scope"]["resources"]["default_effect"] = "allow"  # 非法：必须 deny
    _msg_id, _online_flag, local_issues = a.propose_contract("B", bad)
    assert any("default_effect" in i for i in local_issues), "提议方自检拦下"

    b.poll()
    answer = b.answer()
    assert "未通过" in answer and "default_effect" in answer


def test_tampered_hash_is_detected_on_receipt(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    contract = _contract()
    body = encode_contract_msg("propose", contract=contract, hash="0" * 64)  # 声明与内容不符
    env = Envelope(
        id="t1",
        correlation_id="c1",
        from_="A",
        to="B",
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic="contract", body=body),
        sent_at=1.0,
    )
    DirTransport(tmp_path).deliver("B", codec.encode(env))
    b.poll()
    answer = b.answer()
    assert "hash 不符" in answer


def test_plain_chat_answer_has_no_contract_report(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    a, b = _svc("A", tmp_path, injected), _svc("B", tmp_path, injected)
    a.send("B", "在吗", topic="chat")
    b.poll()
    assert "[机器校验]" not in b.answer()


# ── ack：回执可送达 ─────────────────────────────────────


def test_ack_roundtrip(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    a, b = _svc("A", tmp_path, injected), _svc("B", tmp_path, injected)
    _online(tmp_path, "A", "B")

    msg_id, online = b.ack_contract("A", "ctr-01J", "abc123", accept=True)
    assert msg_id and online is True
    assert a.poll() == 1
    assert "abc123" in a.answer()


# ── 交换区：提议方写（单写者纪律） ───────────────────────


def test_propose_publishes_contract_to_exchange(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    exchange = ContractExchange(tmp_path / "contracts")
    a = _svc("A", tmp_path, injected, exchange=exchange)

    contract = _contract()
    a.propose_contract("B", contract)
    assert exchange.exists("ctr-01J")
    assert contract_digest(exchange.read("ctr-01J")) == contract_digest(contract)


def test_invalid_contract_is_not_published(tmp_path: Path) -> None:
    injected: list[tuple[str, str]] = []
    exchange = ContractExchange(tmp_path / "contracts")
    a = _svc("A", tmp_path, injected, exchange=exchange)

    bad = _contract()
    bad["interfaces"] = []
    a.propose_contract("B", bad)
    assert not exchange.exists("ctr-01J"), "自检不过不落盘"
