"""P2 契约机验收测试：双锁校验 / 机器判兼容 / 交换区写入权。

对应 docs/dev/zeta_契约schema.md 的 P0 门禁：
- test_contract_requires_schema_and_hash（双锁）
- 安全区 fail-closed、作用域规范化、parties ≥2
- 机器判兼容不采信声明
- 交换区不覆盖
"""

from __future__ import annotations

from typing import Any

import pytest

from fp_core.plugins.zeta.contracts import (
    ContractConflictError,
    ContractExchange,
    contract_digest,
    diff_contracts,
    sign_gate,
    validate_contract,
)


def _contract(**over: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "contract_version": "1.0",
        "id": "ctr-01J",
        "name": "items-api",
        "revision": 1,
        "status": "proposed",
        "parties": [
            {"name": "instance-A", "role": "backend"},
            {"name": "instance-B", "role": "frontend"},
        ],
        "scope": {
            "resources": {
                "default_effect": "deny",
                "read": ["repo://server/src/**"],
                "write": ["repo://server/api/**"],
                "deny": ["repo://**/secrets/**"],
            },
            "norm": "path-posix-absolute-v1",
        },
        "interfaces": [
            {
                "id": "get-items",
                "kind": "http",
                "http": {"method": "GET", "path": "/api/items"},
                "semantics": {"idempotent": True, "side_effects": []},
            }
        ],
        "governance": {"change_policy": "append-only-core", "amend_requires": "all-parties"},
    }
    base.update(over)
    return base


# ── 1. 双锁：结构 + 语义 ────────────────────────────────────


def test_valid_contract_passes() -> None:
    assert validate_contract(_contract()) == []


def test_unknown_contract_version_is_fail_closed() -> None:
    issues = validate_contract(_contract(contract_version="9.9"))
    assert any("未知 contract_version" in i for i in issues)


def test_missing_schema_version_rejected() -> None:
    contract = _contract()
    del contract["contract_version"]
    issues = validate_contract(contract)
    assert any("contract_version" in i for i in issues)


# ── 2. 安全区 fail-closed ──────────────────────────────────


def test_safe_namespace_unknown_field_rejected() -> None:
    contract = _contract()
    contract["scope"]["deny_future"] = ["repo://**/x/**"]  # 拟议的未知安全字段
    issues = validate_contract(contract)
    assert any("安全区 scope" in i and "deny_future" in i for i in issues)


def test_interface_semantics_unknown_field_rejected() -> None:
    contract = _contract()
    contract["interfaces"][0]["semantics"]["danger"] = "yes"
    issues = validate_contract(contract)
    assert any("semantics" in i and "danger" in i for i in issues)


# ── 3. 作用域规范化（治 tmp/* 歧义） ───────────────────────


def test_relative_scope_path_rejected() -> None:
    contract = _contract()
    contract["scope"]["resources"]["write"] = ["tmp/*"]
    issues = validate_contract(contract)
    assert any("绝对形式" in i for i in issues)


def test_scope_default_effect_must_be_deny() -> None:
    contract = _contract()
    contract["scope"]["resources"]["default_effect"] = "allow"
    issues = validate_contract(contract)
    assert any("default_effect" in i and "deny" in i for i in issues)


# ── 4. 对等结构（parties ≥2，拒绝二元限死） ────────────────


def test_parties_must_be_at_least_two() -> None:
    issues = validate_contract(_contract(parties=[{"name": "instance-A"}]))
    assert any("parties" in i for i in issues)


def test_parties_accepts_three() -> None:
    three = [{"name": f"instance-{c}", "role": "peer"} for c in "ABC"]
    assert validate_contract(_contract(parties=three)) == []


# ── 5. 规范化与摘要（§9） ──────────────────────────────────


def test_digest_is_stable_across_key_order() -> None:
    a = _contract()
    b = {k: a[k] for k in sorted(a, reverse=True)}
    assert contract_digest(a) == contract_digest(b)


def test_digest_excludes_integrity_field() -> None:
    plain = _contract()
    signed = _contract()
    signed["integrity"] = {"hash": "deadbeef", "sigs": [{"party": "instance-A"}]}
    assert contract_digest(plain) == contract_digest(signed)


# ── 6. 兼容性机器判定（不采信声明，§6） ─────────────────────


def test_added_interface_is_additive() -> None:
    new = _contract()
    new["interfaces"].append({"id": "create-item", "kind": "http", "http": {"method": "POST", "path": "/api/items"}})
    kind, notes = diff_contracts(_contract(), new)
    assert kind == "additive"
    assert any("create-item" in n for n in notes)


def test_unauthorized_removal_is_breaking() -> None:
    new = _contract()
    new["interfaces"] = []
    kind, breaks = diff_contracts(_contract(), new)
    assert kind == "breaking"
    assert any("未授权移除" in b for b in breaks)


def test_authorized_removal_with_conformance_evidence_is_additive() -> None:
    old = _contract(interfaces=[{"id": "legacy", "kind": "http", "http": {"path": "/v1"}, "deprecated": True}])
    new = _contract(
        interfaces=[],
        deprecations=[
            {
                "id": "dep-0001",
                "target": {"pointer": "/interfaces/0/http/path", "was": "/v1"},
                "change": "removal",
                "state": "ready_to_remove",
                "migration": [
                    {
                        "party": "instance-A",
                        "status": "migrated",
                        "evidence": {"kind": "conformance", "test_ref": "test://A/t"},
                    },
                    {
                        "party": "instance-B",
                        "status": "migrated",
                        "evidence": {"kind": "conformance", "test_ref": "test://B/t"},
                    },
                ],
            }
        ],
    )
    kind, notes = diff_contracts(old, new)
    assert kind == "additive"
    assert any("经收缩流程移除" in n for n in notes)


def test_weak_evidence_does_not_authorize_removal() -> None:
    old = _contract(interfaces=[{"id": "legacy", "kind": "http", "http": {"path": "/v1"}}])
    new = _contract(
        interfaces=[],
        deprecations=[
            {
                "id": "dep-0001",
                "target": {"pointer": "/interfaces/0/http/path"},
                "change": "removal",
                "state": "ready_to_remove",
                "migration": [
                    {"party": "instance-A", "status": "migrated", "evidence": {"kind": "declared"}},
                ],
            }
        ],
    )
    kind, breaks = diff_contracts(old, new)
    assert kind == "breaking"
    assert any("未授权移除" in b for b in breaks)


def test_kind_change_is_breaking() -> None:
    new = _contract()
    new["interfaces"][0]["http"]["path"] = "/api/v2/items"
    kind, breaks = diff_contracts(_contract(), new)
    assert kind == "breaking"
    assert any("签名变更" in b for b in breaks)


# ── 7. 落盘闸门：声明必须与机器判定一致 ────────────────────


def test_sign_gate_rejects_hash_mismatch() -> None:
    issues = sign_gate(
        _contract(),
        expected_hash="0" * 64,
        machine_reading="additive",
        declared_change_kind="additive",
    )
    assert any("hash 不一致" in i for i in issues)


def test_sign_gate_rejects_lying_change_kind() -> None:
    contract = _contract()
    issues = sign_gate(
        contract,
        expected_hash=contract_digest(contract),
        machine_reading="breaking",
        declared_change_kind="additive",
    )
    assert any("不符" in i for i in issues)


# ── 8. 交换区写入权（不覆盖） ──────────────────────────────


def test_exchange_publish_then_duplicate(tmp_path) -> None:
    exchange = ContractExchange(tmp_path / "contracts")
    action, path = exchange.publish(_contract())
    assert action == "written" and path.exists()
    again, _ = exchange.publish(_contract())
    assert again == "duplicate"


def test_exchange_refuses_overwrite_with_different_content(tmp_path) -> None:
    exchange = ContractExchange(tmp_path / "contracts")
    exchange.publish(_contract())
    mutated = _contract(revision=2)
    with pytest.raises(ContractConflictError):
        exchange.publish(mutated)


def test_exchange_requires_id(tmp_path) -> None:
    exchange = ContractExchange(tmp_path / "contracts")
    with pytest.raises(ValueError, match="缺 id"):
        exchange.publish({"revision": 1})
