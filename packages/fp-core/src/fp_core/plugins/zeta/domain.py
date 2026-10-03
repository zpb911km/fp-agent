"""Zeta 邻居领域模型（IR）—— 协议无关层。

分层门禁
    本模块及其 import 图中**不得出现** a2a / httpx / socket / requests / 任何
    codec 与传输库。验收判据：删掉全部 codec 后，本层测试必须全绿。

信任红线
    **LLM 的输出不是可信输入。** 凡进入本层的状态（契约、卡、协商结论），
    必须存在一条非 LLM 的确定性校验路径——信任根不许放在任何实例的嘴里。

本层只承载自描述数据与纯函数判定（协商/校验/裁决），不承载传输语义。
codec 层负责字节映射（见 zeta_契约schema.md §6 映射锚点表）。
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any, cast

# ─────────────────────────────────────────────────────────
#  枚举
# ─────────────────────────────────────────────────────────


class NeighborKind(StrEnum):
    """邻居种类：本协议实例 or 说 A2A 的外部异构 agent。"""

    FP_INSTANCE = "FP_INSTANCE"
    EXTERNAL_A2A_AGENT = "EXTERNAL_A2A_AGENT"


class TrustLevel(StrEnum):
    """信任档（§1.1）。升级只由本地策略决定，不凭对端声明。"""

    TRUSTED_PEER = "TRUSTED_PEER"  # 同机熟人
    KNOWN = "KNOWN"  # 互访过
    STRANGER = "STRANGER"  # 裸外部


class PeerStatus(StrEnum):
    """运行状态（名片字段，供打电话的人判断忙不忙）。"""

    IDLE = "idle"
    BUSY = "busy"
    AWAY = "away"


class MsgKind(StrEnum):
    """消息大类。分流是成本与安全的双闸门：MECHANICAL 永不进 LLM。"""

    MECHANICAL = "MECHANICAL"
    SEMANTIC = "SEMANTIC"


class InjectAs(StrEnum):
    """注入通道。"""

    USER_MESSAGE = "USER_MESSAGE"
    SYSTEM_CONTEXT = "SYSTEM_CONTEXT"
    TOOL_RESULT = "TOOL_RESULT"
    NOTIFICATION = "NOTIFICATION"


class ContentTrust(StrEnum):
    """内容可信标签。发送方填写、接收方覆盖（接收方永远有权降级）。"""

    TRUSTED_PEER = "TRUSTED_PEER"
    UNTRUSTED_CONTENT = "UNTRUSTED_CONTENT"


class ProfileState(StrEnum):
    """Profile 协商状态机（机械层）。"""

    NOT_STARTED = "NOT_STARTED"
    PROBING = "PROBING"
    NEGOTIATED = "NEGOTIATED"
    DEGRADED = "DEGRADED"
    FAILED = "FAILED"


class ContractState(StrEnum):
    """契约协商状态机（语义层，双锁）。"""

    INIT = "INIT"
    PROPOSED = "PROPOSED"
    AGREED = "AGREED"
    REJECTED = "REJECTED"
    FAILED = "FAILED"


# ─────────────────────────────────────────────────────────
#  规范化与摘要（hash 共识的物理前提）
# ─────────────────────────────────────────────────────────

# 参与摘要的状态字段：排除摘要与签名自身（自指会破坏可复算性）。
_DIGEST_EXCLUDED = frozenset({"card_hash", "sig"})


def canonical_json(obj: Any) -> str:
    """确定性 JSON 序列化（JCS 风格简化版）。

    键排序 + 紧凑分隔符 —— 同一逻辑值必得同一字节串，这是 hash 锁字节的前提。
    TODO(§9): 完整 RFC 8785 JCS（数字/Unicode 规范形式）待契约 schema P2 引入。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def digest_of(payload: dict[str, Any]) -> str:
    """对自描述数据求摘要（自动排除摘要/签名字段）。"""
    pruned = {k: v for k, v in payload.items() if k not in _DIGEST_EXCLUDED}
    return sha256_hex(canonical_json(pruned))


# ─────────────────────────────────────────────────────────
#  邻居卡（§1）
# ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Endpoint:
    """一个可达端点。多端点 + priority：local:// | tcp:// | frp://"""

    kind: str
    address: str
    priority: int = 100


@dataclass
class Capabilities:
    """技术能力声明：只声明「我接得住什么」，不声明角色与意图。"""

    profiles: list[str] = field(default_factory=list[str])
    semantic_layers: str = "MECHANICAL_ONLY"  # MECHANICAL_ONLY | SEMANTIC_CAPABLE
    max_envelope_bytes: int | None = None
    supports_idempotency: bool = False
    skills: list[str] = field(default_factory=list[str])  # 能力协商（异构降级依据）

    def to_dict(self) -> dict[str, Any]:
        return {
            "profiles": list(self.profiles),
            "semantic_layers": self.semantic_layers,
            "max_envelope_bytes": self.max_envelope_bytes,
            "supports_idempotency": self.supports_idempotency,
            "skills": list(self.skills),
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Capabilities:
        return cls(
            profiles=list(d.get("profiles") or []),
            semantic_layers=str(d.get("semantic_layers") or "MECHANICAL_ONLY"),
            max_envelope_bytes=d.get("max_envelope_bytes"),
            supports_idempotency=bool(d.get("supports_idempotency", False)),
            skills=list(d.get("skills") or []),
        )


@dataclass
class NeighborCard:
    """邻居名片 = 发现条目 + 路由表（§1.4 名片即路由表）。

    同机发现目录：``~/.local/share/fp/peers/<name>.json``；
    **每实例只许写自己的卡**（§1.2）。跨机时 endpoint 换 frp 地址，卡不变。
    """

    name: str
    kind: NeighborKind = NeighborKind.FP_INSTANCE
    # ── 业务身份（路由依据）──
    workspace: str = ""  # workspace 根（项目归属，路由键用）；C(a)= 启动目录
    cwd: str = ""  # 进程当前目录（属性，不参与路由）
    business: str = ""
    tags: list[str] = field(default_factory=list[str])
    # ── 运行状态 ──
    status: PeerStatus = PeerStatus.IDLE
    current_task: str | None = None
    started_at: float = 0.0
    # ── 技术能力 ──
    endpoints: list[Endpoint] = field(default_factory=list[Endpoint])
    codec: str = "a2a"
    capabilities: Capabilities = field(default_factory=Capabilities)
    roles: list[str] = field(default_factory=list[str])  # 自我声明立场（非指派）
    trust: TrustLevel = TrustLevel.STRANGER
    heartbeat: float = 0.0
    issued_at: float = 0.0
    card_hash: str = ""
    sig: str | None = None

    # ── 序列化 ──

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "kind": self.kind.value,
            "workspace": self.workspace,
            "cwd": self.cwd,
            "business": self.business,
            "tags": list(self.tags),
            "status": self.status.value,
            "current_task": self.current_task,
            "started_at": self.started_at,
            "endpoints": [{"kind": e.kind, "address": e.address, "priority": e.priority} for e in self.endpoints],
            "codec": self.codec,
            "capabilities": self.capabilities.to_dict(),
            "roles": list(self.roles),
            "trust": self.trust.value,
            "heartbeat": self.heartbeat,
            "issued_at": self.issued_at,
            "card_hash": self.card_hash,
            "sig": self.sig,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> NeighborCard:
        return cls(
            name=str(d["name"]),
            kind=NeighborKind(d.get("kind", NeighborKind.FP_INSTANCE.value)),
            workspace=str(d.get("workspace") or ""),
            cwd=str(d.get("cwd") or ""),
            business=str(d.get("business") or ""),
            tags=list(d.get("tags") or []),
            status=PeerStatus(d.get("status", PeerStatus.IDLE.value)),
            current_task=d.get("current_task"),
            started_at=float(d.get("started_at") or 0.0),
            endpoints=[
                Endpoint(
                    kind=str(e.get("kind", "")),
                    address=str(e.get("address", "")),
                    priority=int(e.get("priority", 100)),
                )
                for e in cast(list[dict[str, Any]], d.get("endpoints") or [])
            ],
            codec=str(d.get("codec") or "a2a"),
            capabilities=Capabilities.from_dict(d.get("capabilities") or {}),
            roles=list(d.get("roles") or []),
            trust=TrustLevel(d.get("trust", TrustLevel.STRANGER.value)),
            heartbeat=float(d.get("heartbeat") or 0.0),
            issued_at=float(d.get("issued_at") or 0.0),
            card_hash=str(d.get("card_hash") or ""),
            sig=d.get("sig"),
        )

    # ── 摘要 ──

    def compute_hash(self) -> str:
        return digest_of(self.to_dict())

    def is_online(self, now: float | None = None, ttl: float = 90.0) -> bool:
        """心跳新鲜即在线（ttl 建议 = 3× 心跳周期）。"""
        t = time.time() if now is None else now
        return (t - self.heartbeat) <= ttl

    def route_key(self) -> tuple[list[str], str, str]:
        """路由依据（§1.4 优先级）：tags > business > workspace。

        workspace（项目归属）取代 cwd 成为「同项目」判据——cwd 只是进程属性。
        """
        return (list(self.tags), self.business, self.workspace)


def verify_card(card: NeighborCard) -> list[str]:
    """确定性校验一张卡（非 LLM）。返回 issues，空 = 通过。

    - card_hash 必须与内容一致（防篡改/半写）；
    - 必填字段非空；
    - 端点地址合法（非空）；
    签名验证（sig）留待 P3 引入密钥设施；STRANGER 互访时强制。
    """
    issues: list[str] = []
    if not card.name:
        issues.append("name 为空")
    if card.card_hash and card.card_hash != card.compute_hash():
        issues.append("card_hash 与内容不符（疑似篡改/半写）")
    for e in card.endpoints:
        if not e.address:
            issues.append(f"端点 {e.kind} 地址为空")
    if not card.capabilities.profiles and card.kind is NeighborKind.FP_INSTANCE:
        issues.append("capabilities.profiles 为空（FP 实例必须声明协商语言）")
    # 外部 A2A agent 允许不声明 profile——天然降级到 bare-a2a，不是无效卡。
    return issues


# ─────────────────────────────────────────────────────────
#  消息信封（§2）
# ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class Ttl:
    deadline: float | None = None  # unix ts；None = 无期限
    max_hops: int = 1  # MVP 单跳，多跳转发预留（路由器方向 P3）

    def expired(self, now: float | None = None) -> bool:
        if self.deadline is None:
            return False
        return (time.time() if now is None else now) >= self.deadline


@dataclass(frozen=True)
class Part:
    """Artifact 多 part：text / file / data。"""

    type: str
    text: str | None = None
    uri: str | None = None
    data: dict[str, Any] | None = None


@dataclass(frozen=True)
class InjectionHint:
    inject_as: InjectAs = InjectAs.USER_MESSAGE
    trust_label: ContentTrust = ContentTrust.UNTRUSTED_CONTENT
    max_context_tokens: int | None = None


@dataclass
class Payload:
    kind_mech: str | None = None  # MECHANICAL："profile_probe"|"ack"|"nack"|"offline"|...
    topic: str | None = None  # SEMANTIC："contract"|"task"|"chat"
    body: str | None = None
    parts: list[Part] | None = None
    injection: InjectionHint | None = None


@dataclass
class Envelope:
    """消息信封。IR 只描述形状，不解释 codec_meta。"""

    id: str
    correlation_id: str
    from_: str
    to: str
    kind: MsgKind
    payload: Payload
    reply_to: str | None = None
    idempotency_key: str | None = None
    ttl: Ttl = field(default_factory=Ttl)
    sent_at: float = 0.0
    codec_meta: dict[str, Any] | None = None  # codec 专属，IR 视为不透明 kv
    sig: str | None = None

    def should_inject(self) -> bool:
        """只有 SEMANTIC 注入实例；MECHANICAL 由网关直接应答（红线 #2）。"""
        return self.kind is MsgKind.SEMANTIC


# ─────────────────────────────────────────────────────────
#  Profile 协商（§3，机械层，LLM 不参与）
# ─────────────────────────────────────────────────────────


@dataclass(frozen=True)
class ProfileNegotiation:
    state: ProfileState
    selected: str | None = None
    degraded: bool = False


def negotiate_profile(
    local: list[str],
    remote: list[str],
    priority: list[str] | None = None,
    downgrade_chain: list[str] | None = None,
) -> ProfileNegotiation:
    """取交集选 priority 最高者；无交集走降级链；再无 → FAILED。

    ``priority`` 只列「首选档」（语义完整能力）；``bare-a2a`` 属降级链，
    命中它 = DEGRADED（降带宽，不降信任检查——§1.1）。
    """
    prio = priority or ["fp-contract-profile"]
    common = [p for p in prio if p in local and p in remote]
    if common:
        return ProfileNegotiation(ProfileState.NEGOTIATED, common[0])
    for p in downgrade_chain or ["bare-a2a"]:
        if p in local and p in remote:
            return ProfileNegotiation(ProfileState.DEGRADED, p, degraded=True)
    return ProfileNegotiation(ProfileState.FAILED, None)


def probe_leader(name_a: str, name_b: str) -> str:
    """同时发起 probe 的裁决：字典序小者主导（确定性规则，非竞争）。"""
    return name_a if name_a <= name_b else name_b


# ─────────────────────────────────────────────────────────
#  契约协商（§4，语义层，双锁）
# ─────────────────────────────────────────────────────────

# P0 结构性必填项；完整 fp-contract-profile schema 见 zeta_契约schema.md（P2 补齐）。
_CONTRACT_REQUIRED = ("contract_version", "schema_version", "interface", "scope", "governance")


def contract_hash(contract: dict[str, Any]) -> str:
    """契约摘要：JCS 规范化后的 sha256（hash 锁字节）。"""
    return digest_of(contract)


def validate_contract_structure(contract: dict[str, Any]) -> list[str]:
    """确定性结构校验（非 LLM）。返回 issues，空 = 通过。

    这是「schema 锁语义」的第一道闸门：不过 = 不落盘、不可 AGREED。
    """
    issues: list[str] = []
    for key in _CONTRACT_REQUIRED:
        if key not in contract:
            issues.append(f"缺必填字段: {key}")
    iface = contract.get("interface")
    if not isinstance(iface, list) or not iface:
        issues.append("interface 必须是非空数组")
    else:
        for i, item in enumerate(cast(list[Any], iface)):
            if not isinstance(item, dict):
                issues.append(f"interface[{i}] 必须是对象")
                continue
            entry = cast(dict[str, Any], item)
            if not entry.get("kind"):
                issues.append(f"interface[{i}] 缺 kind（自由文本不得定义安全边界）")
            if not isinstance(entry.get("fields"), list) and entry.get("kind") != "route":
                issues.append(f"interface[{i}] 缺 fields 数组")
    return issues


def can_agree(proposed_hash: str, acked_hash: str, contract_issues: list[str]) -> bool:
    """双锁判定：hash 相等（锁字节）且结构校验通过（锁语义）。

    无论双方 LLM 说了多少个「就这么定」，此函数是唯一裁决者。
    """
    return proposed_hash == acked_hash and not contract_issues


# ─────────────────────────────────────────────────────────
#  幂等窗口（§5，离线/重复）
# ─────────────────────────────────────────────────────────


class IdempotencyWindow:
    """(idempotency_key) → cached_response；保留 ≥ TTL（建议 24h）。

    机械消息天然幂等无需键；语义消息重投/重放共享键。
    """

    def __init__(self, ttl_seconds: float = 86400.0) -> None:
        self._ttl = ttl_seconds
        self._cache: dict[str, tuple[float, str | None]] = {}
        self._inflight: set[str] = set()

    def _purge(self, now: float) -> None:
        stale = [k for k, (ts, _) in self._cache.items() if now - ts > self._ttl]
        for k in stale:
            self._cache.pop(k, None)

    def check(self, key: str | None, now: float | None = None) -> tuple[str, str | None] | None:
        """返回 ("cached", response) / ("inflight", None) / None（未见）。"""
        if not key:
            return None
        t = time.time() if now is None else now
        self._purge(t)
        if key in self._cache:
            return ("cached", self._cache[key][1])
        if key in self._inflight:
            return ("inflight", None)
        self._inflight.add(key)
        return None

    def remember(self, key: str | None, response: str | None = None, now: float | None = None) -> None:
        if not key:
            return
        t = time.time() if now is None else now
        self._inflight.discard(key)
        self._cache[key] = (t, response)


# ─────────────────────────────────────────────────────────
#  注入策略（§7 的二跳注入面防护）
# ─────────────────────────────────────────────────────────


def make_injection(
    trust: TrustLevel,
    content_len: int,
    max_tokens: int | None = None,
) -> InjectionHint:
    """按信任档生成注入提示。

    UNTRUSTED_CONTENT 走 USER_MESSAGE + 显式边界包裹（见 render_untrusted）。
    """
    label = ContentTrust.UNTRUSTED_CONTENT if trust is TrustLevel.STRANGER else ContentTrust.TRUSTED_PEER
    return InjectionHint(
        inject_as=InjectAs.USER_MESSAGE,
        trust_label=label,
        max_context_tokens=max_tokens,
    )


UNTRUSTED_OPEN = "<untrusted_peer_content>"
UNTRUSTED_CLOSE = "</untrusted_peer_content>"


def render_untrusted(body: str) -> str:
    """不可信内容必须显式边界包裹（防止二跳注入污染本地 prompt）。"""
    return f"{UNTRUSTED_OPEN}\n{body}\n{UNTRUSTED_CLOSE}"


def ring_line(peer: str, topic: str, waited_seconds: float) -> str:
    """铃声内容（来电显示级，不含正文）。"""
    mins = int(waited_seconds // 60)
    waited = f"{mins}m" if mins else f"{int(waited_seconds)}s"
    return f"【铃声】peer:{peer} 在找你（主题={topic}，已等 {waited}）→ 接听调 peer_answer"


def _fmt_age(seconds: float) -> str:
    """心跳年龄的人类可读格式。"""
    s = max(0, int(seconds))
    if s < 60:
        return f"{s}s"
    m = s // 60
    if m < 60:
        return f"{m}m"
    return f"{m // 60}h{m % 60}m"


def neighbor_brief(card: NeighborCard, now: float | None = None) -> str:
    """发现简报（§1.3 发现必须告知 LLM）。

    带心跳年龄 ``hb``：邻居状态时变，年龄让 LLM 一眼看出结果的新鲜度，
    抑制「拿上下文里旧结果当缓存」的行为。
    """
    t = time.time() if now is None else now
    age = _fmt_age(t - card.heartbeat) if card.heartbeat else "?"
    tags = ",".join(card.tags) or "-"
    cur = f" 当前={card.current_task}" if card.current_task else ""
    # 外部异构对端：识别必须对 LLM 可见（否则「能跟非 FP 说话」等于没说）。
    extern = (
        "  ⚠外部A2A对端（非本协议实例，契约结论将标 UNVERIFIED）"
        if card.kind is NeighborKind.EXTERNAL_A2A_AGENT
        else ""
    )
    return (
        f"【邻居】peer:{card.name} 上线\n"
        f"  ws={card.workspace or card.cwd or '-'}  business={card.business or '-'}\n"
        f"  tags=[{tags}]  status={card.status.value}  hb={age}{cur}{extern}"
    )
