"""契约机（P2）：双锁校验、机器判兼容、交换区写入权。

设计依据：docs/dev/zeta_契约schema.md（v0.2）。

信任红线（全文最重要的一句）：
    LLM 只产候选；一致性由 hash 判（锁字节），正确性由 schema 判（锁语义）。
本模块不含任何 LLM 调用——它是把信任根从 LLM 手里拿回来的那一层。
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from .domain import canonical_json, sha256_hex

# ─────────────────────────────────────────────────────────
#  规范化与摘要（§9：hash 共识的物理前提）
# ─────────────────────────────────────────────────────────


def jcs_canonical(obj: Any) -> str:
    """确定性 JSON 序列化（RFC 8785 JCS 风格）。

    键序 + 紧凑分隔符 + 非 ASCII 直出——同一逻辑值必得同一字节串。
    这是「hash 锁字节」成立的前提：差一个空格，hash 就永远对不上。
    """
    return canonical_json(obj)


def contract_digest(contract: dict[str, Any]) -> str:
    """契约摘要。``integrity`` 字段自身不参与（自指会破坏可复算性，§9）。"""
    body = {key: value for key, value in contract.items() if key != "integrity"}
    return sha256_hex(jcs_canonical(body))


# ─────────────────────────────────────────────────────────
#  确定性校验器（§2.3 / §3 / §4 / §10）
# ─────────────────────────────────────────────────────────

# 路径必须规范化：scheme://... 绝对形式（拒绝 tmp/* 这类相对/歧义写法）。
_SCHEME_RE = re.compile(r"^[a-z][a-z0-9+.\-]*://")

# 安全命名空间的已知字段白名单：未知字段 fail-closed（宁可不达成，不可误放行）。
_SAFE_KNOWN: dict[str, frozenset[str]] = {
    "scope": frozenset({"resources", "norm", "target"}),
    "resources": frozenset({"default_effect", "read", "write", "deny"}),
    "governance": frozenset({"change_policy", "amend_requires", "deprecation_window_days", "termination"}),
    "semantics": frozenset({"idempotent", "side_effects", "consistency", "cacheable"}),
}

_REQUIRED_TOP = (
    "contract_version",
    "id",
    "name",
    "revision",
    "status",
    "parties",
    "scope",
    "interfaces",
)


def _unknown_fields(namespace: str, obj: dict[str, Any], known: frozenset[str]) -> list[str]:
    return [f"安全区 {namespace} 出现未知字段: {key}（fail-closed 拒绝）" for key in obj if key not in known]


@dataclass(frozen=True)
class ContractValidatorV1:
    """contract_version 1.0 的确定性校验器（与 schema 版本绑定，不与代码库绑定）。"""

    version: str = "1.0"

    def validate(self, contract: dict[str, Any]) -> list[str]:
        issues: list[str] = []
        for key in _REQUIRED_TOP:
            if key not in contract:
                issues.append(f"缺必填字段: {key}")

        parties = contract.get("parties")
        if not isinstance(parties, list) or len(cast(list[Any], parties)) < 2:
            issues.append("parties 必须是 ≥2 的数组（对等：拒绝二元限死，§1）")

        issues += self._validate_scope(contract.get("scope"))
        issues += self._validate_interfaces(contract.get("interfaces"))
        return issues

    def _validate_scope(self, scope: Any) -> list[str]:
        if not isinstance(scope, dict):
            return ["scope 必须存在（权限声明的唯一处，§4）"]
        scope_obj = cast(dict[str, Any], scope)
        issues = _unknown_fields("scope", scope_obj, _SAFE_KNOWN["scope"])
        resources = scope_obj.get("resources")
        if not isinstance(resources, dict):
            issues.append("scope.resources 必须存在")
            return issues
        res_obj = cast(dict[str, Any], resources)
        issues += _unknown_fields("scope.resources", res_obj, _SAFE_KNOWN["resources"])
        if res_obj.get("default_effect") != "deny":
            issues.append("scope.resources.default_effect 必须显式声明 deny（fail-closed）")
        for bucket in ("read", "write", "deny"):
            paths = res_obj.get(bucket, [])
            if not isinstance(paths, list):
                issues.append(f"scope.resources.{bucket} 必须是数组")
                continue
            for path in cast(list[Any], paths):
                if not isinstance(path, str) or not _SCHEME_RE.match(path):
                    issues.append(f"作用域路径必须为绝对形式（scheme://...）: {path!r}")
        return issues

    def _validate_interfaces(self, interfaces: Any) -> list[str]:
        if not isinstance(interfaces, list) or not cast(list[Any], interfaces):
            return ["interfaces 必须是非空数组"]
        issues: list[str] = []
        for index, item in enumerate(cast(list[Any], interfaces)):
            if not isinstance(item, dict):
                issues.append(f"interfaces[{index}] 必须是对象")
                continue
            entry = cast(dict[str, Any], item)
            for key in ("id", "kind"):
                if not entry.get(key):
                    issues.append(f"interfaces[{index}] 缺 {key}")
            semantics = entry.get("semantics")
            if isinstance(semantics, dict):
                issues += _unknown_fields(
                    f"interfaces[{index}].semantics",
                    cast(dict[str, Any], semantics),
                    _SAFE_KNOWN["semantics"],
                )
        return issues


# 校验器注册表（§10）：新增 kind / 新版本 = 注册新校验器，不修改既有校验器。
_VALIDATORS: dict[str, ContractValidatorV1] = {ContractValidatorV1().version: ContractValidatorV1()}


def register_validator(validator: ContractValidatorV1) -> None:
    _VALIDATORS[validator.version] = validator


def get_validator(version: str) -> ContractValidatorV1 | None:
    return _VALIDATORS.get(version)


def validate_contract(contract: dict[str, Any]) -> list[str]:
    """按 contract_version 选校验器并校验；返回 issues（空 = 通过）。

    未知版本 = 无可信校验器 → fail-closed 拒绝（§2.3 的信任姿态）。
    """
    version = contract.get("contract_version")
    if not isinstance(version, str):
        return ["缺 contract_version（无从选择校验器，fail-closed）"]
    validator = _VALIDATORS.get(version)
    if validator is None:
        return [f"未知 contract_version: {version!r}（无可信校验器，fail-closed）"]
    return validator.validate(contract)


# ─────────────────────────────────────────────────────────
#  兼容性：机器判定，不采信声明（§6 / §2.1）
# ─────────────────────────────────────────────────────────

# 参与「接口签名」的 kind 专属子对象：任一变化即视为破坏性。
_SIG_KINDS = ("kind", "http", "file", "message", "function")


def _iface_index(contract: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    interfaces = contract.get("interfaces")
    if isinstance(interfaces, list):
        for item in cast(list[Any], interfaces):
            if isinstance(item, dict):
                entry = cast(dict[str, Any], item)
                iface_id = entry.get("id")
                if isinstance(iface_id, str):
                    out[iface_id] = entry
    return out


def _iface_sig(entry: dict[str, Any]) -> str:
    return jcs_canonical({key: entry[key] for key in _SIG_KINDS if key in entry})


def _pointer_head_index(pointer: str) -> int | None:
    match = re.match(r"^/interfaces/(\d+)", pointer)
    return int(match.group(1)) if match else None


def _index_of_id(contract: dict[str, Any], iface_id: str) -> int:
    interfaces = contract.get("interfaces")
    if isinstance(interfaces, list):
        for index, item in enumerate(cast(list[Any], interfaces)):
            if isinstance(item, dict) and cast(dict[str, Any], item).get("id") == iface_id:
                return index
    return -1


def _removal_authorized(old: dict[str, Any], new: dict[str, Any], removed_id: str) -> bool:
    """收缩授权（§2.1）：state ∈ {ready_to_remove, removed} + 全员 migrated + 证据达强级。"""
    deps = new.get("deprecations")
    if not isinstance(deps, list):
        return False
    old_index = _index_of_id(old, removed_id)
    if old_index < 0:
        return False
    for item in cast(list[Any], deps):
        if not isinstance(item, dict):
            continue
        dep = cast(dict[str, Any], item)
        if dep.get("state") not in ("ready_to_remove", "removed"):
            continue
        target = dep.get("target")
        if not isinstance(target, dict):
            continue
        pointer = cast(dict[str, Any], target).get("pointer")
        if not isinstance(pointer, str) or _pointer_head_index(pointer) != old_index:
            continue
        migration = dep.get("migration")
        if not isinstance(migration, list) or not cast(list[Any], migration):
            continue
        if all(_is_strong_migration(row) for row in cast(list[Any], migration)):
            return True
    return False


def _is_strong_migration(row: Any) -> bool:
    if not isinstance(row, dict):
        return False
    row_obj = cast(dict[str, Any], row)
    if row_obj.get("status") != "migrated":
        return False
    evidence = row_obj.get("evidence")
    return isinstance(evidence, dict) and cast(dict[str, Any], evidence).get("kind") == "conformance"


def diff_contracts(old: dict[str, Any], new: dict[str, Any]) -> tuple[str, list[str]]:
    """确定性兼容性判定。返回 (additive|breaking, 说明)。

    ``change_kind`` 是提交方写的、LLM 产出的——不可信；本函数是裁决者（§6）。
    """
    breaks: list[str] = []
    notes: list[str] = []
    old_ifaces, new_ifaces = _iface_index(old), _iface_index(new)

    for removed in sorted(set(old_ifaces) - set(new_ifaces)):
        if _removal_authorized(old, new, removed):
            notes.append(f"经收缩流程移除: {removed}")
        else:
            breaks.append(f"接口被未授权移除（无强级迁移证据）: {removed}")
    for shared in sorted(set(old_ifaces) & set(new_ifaces)):
        if _iface_sig(old_ifaces[shared]) != _iface_sig(new_ifaces[shared]):
            breaks.append(f"接口签名变更（kind/协议专属）: {shared}")

    if breaks:
        return "breaking", breaks
    notes += [f"新增接口: {added}" for added in sorted(set(new_ifaces) - set(old_ifaces))]
    return "additive", notes


def sign_gate(
    contract: dict[str, Any],
    *,
    expected_hash: str,
    machine_reading: str,
    declared_change_kind: str,
) -> list[str]:
    """落盘前的最后一道闸门（双锁 + 声明一致性）。

    - hash 必须与 canonical 字节一致（锁字节）；
    - 结构/作用域/安全区校验必须通过（锁语义）；
    - ``change_kind`` 声明必须与机器判定一致（声明与判定不符 → 拒绝）。
    """
    issues = validate_contract(contract)
    actual_hash = contract_digest(contract)
    if actual_hash != expected_hash:
        issues.append(f"hash 不一致: 期望 {expected_hash[:12]}… 实得 {actual_hash[:12]}…")
    if declared_change_kind != machine_reading:
        issues.append(f"change_kind 声明({declared_change_kind}) 与机器判定({machine_reading}) 不符")
    return issues


# ─────────────────────────────────────────────────────────
#  交换区（§4 写入权：提议方写、确认方只读校验）
# ─────────────────────────────────────────────────────────


class ContractConflictError(RuntimeError):
    """已存在同名契约且内容不同——拒绝覆盖（不覆盖是写入权纪律的物理保障）。"""


def _safe_slug(text: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")
    return slug or "contract"


class ContractExchange:
    """契约交换区：磁盘上的「共同签署处」。

    单写者纪律：落盘用 ``O_EXCL`` 创建，已存在则**比 hash**——相同视为幂等重投，
    不同则拒绝（不做 last-write-wins）。确认方只读，不改写提议方的文件。
    """

    def __init__(self, root: Path) -> None:
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)

    def path_for(self, contract_id: str) -> Path:
        return self.root / f"{_safe_slug(contract_id)}.json"

    def exists(self, contract_id: str) -> bool:
        return self.path_for(contract_id).exists()

    def publish(self, contract: dict[str, Any]) -> tuple[str, Path]:
        """提议方写入。返回 ("written"|"duplicate", path)；冲突抛 ContractConflict。"""
        contract_id = contract.get("id")
        if not isinstance(contract_id, str) or not contract_id:
            raise ValueError("契约缺 id，不可落盘")
        path = self.path_for(contract_id)
        body = jcs_canonical(contract)
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            existing = self.read(contract_id)
            if contract_digest(existing) == contract_digest(contract):
                return "duplicate", path
            raise ContractConflictError(f"契约 {contract_id} 已存在且内容不同：拒绝覆盖") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(body)
        except Exception:
            path.unlink(missing_ok=True)
            raise
        return "written", path

    def read(self, contract_id: str) -> dict[str, Any]:
        path = self.path_for(contract_id)
        raw = json.loads(path.read_text("utf-8"))
        if not isinstance(raw, dict):
            raise ValueError(f"契约文件损坏（非对象）: {path}")
        return cast(dict[str, Any], raw)
