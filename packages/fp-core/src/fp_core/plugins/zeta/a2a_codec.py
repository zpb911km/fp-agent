"""Zeta A2A codec —— IR ↔ A2A (Agent2Agent) wire 映射（翻译面，可插拔）。

用途：与非 FP 的**异构 agent** 互联。只借 A2A 的词汇与 wire 形状（AgentCard /
Message），**不借其零信任 / OAuth 世界观**（见 zeta_协议总览.md）。

两条关键语义：

- **外部 = 天然降级**。标准 A2A AgentCard 不声明 ``fp-contract-profile``，
  映射出的 ``capabilities.profiles`` 为空 → ``negotiate_profile`` 必然走降级链
  （``bare-a2a``，见 domain）。若对端是「也用 A2A 暴露的 FP」，则在
  ``capabilities.extensions`` 里带 :data:`FP_PROFILE_URI`，协商即可 NEGOTIATED。
- **外部内容 = 不可信输入**。``a2a_message_to_envelope`` 一律把
  ``trust_label`` 置为 ``UNTRUSTED_CONTENT``：接收方永远有权降级信任（§1.1）。

分层：本模块属 codec 层，允许触碰协议细节；领域层不得 import 本模块。
"""

from __future__ import annotations

import time
import uuid
from typing import Any, cast

from fp_core.plugins.zeta.domain import (
    Capabilities,
    ContentTrust,
    Endpoint,
    Envelope,
    InjectAs,
    InjectionHint,
    MsgKind,
    NeighborCard,
    NeighborKind,
    Payload,
    PeerStatus,
    TrustLevel,
    Ttl,
)

# 本协议在 A2A 之上声明的 profile（以 URI 命名空间承载，避免字符串漂移）
FP_PROFILE_URI = "urn:fp:zeta:profile:fp-contract-profile"
FP_CONTRACT_PROFILE = "fp-contract-profile"
BARE_A2A = "bare-a2a"  # 任何 A2A 对端的底线能力（能收发一条消息）

_URI_TO_PROFILE = {FP_PROFILE_URI: FP_CONTRACT_PROFILE}

# ── wire 版本相关常量 ───────────────────────────────────
#
# **实测（2026-10，against a2a-sdk 1.1.0 / helloworld）**：A2A 1.0 是一次破坏性
# 升级，与 0.x 的 wire 形状多处不兼容。差异必须显式编码，不能靠猜：
#
#   | 维度       | v0.x（旧）               | v1.0（新，实测）                        |
#   |-----------|--------------------------|----------------------------------------|
#   | card 路径  | /.well-known/agent.json  | /.well-known/agent-card.json           |
#   | card 端点  | 顶层 ``url``              | ``supportedInterfaces[].url``          |
#   | 方法名     | ``message/send``         | ``SendMessage``                        |
#   | 版本协商   | 无                       | HTTP 头 ``A2A-Version: 1.0``（必需）    |
#   | role       | ``user``/``agent``       | ``ROLE_USER``/``ROLE_AGENT``           |
#   | part       | ``{kind:"text", text:…}``| ``{text:…, mediaType:…}``（扁平）       |
#
# 出站按对端 ``protocolVersion`` 选形状；入站解析一律**宽松**（两种都认）。
PROTOCOL_V1 = "1.0"
CARD_PATH_V1 = "/.well-known/agent-card.json"
CARD_PATH_V0 = "/.well-known/agent.json"
VERSION_HEADER = "A2A-Version"
METHOD_SEND_MESSAGE = "SendMessage"

# 语义消息默认存活期（与 peer.DEFAULT_TTL_SECONDS 对齐；此处避免循环 import 而重声明）
_DEFAULT_TTL_SECONDS = 7 * 86400.0


# ── AgentCard ↔ NeighborCard ─────────────────────────────


def _extension_uris(capabilities: dict[str, Any]) -> list[str]:
    """抽取 A2A ``capabilities.extensions`` 的 URI 列表（兼容字符串/对象两种写法）。"""
    uris: list[str] = []
    for e in cast(list[Any], capabilities.get("extensions") or []):
        if isinstance(e, str):
            uris.append(e)
        elif isinstance(e, dict):
            u = cast(dict[str, Any], e).get("uri")
            if isinstance(u, str):
                uris.append(u)
    return uris


def _endpoint_url(a2a: dict[str, Any]) -> str:
    """从 AgentCard 抽取端点 URL：兼容 v1.0 ``supportedInterfaces`` 与 v0.x 顶层 ``url``。"""
    for it in cast(list[Any], a2a.get("supportedInterfaces") or []):
        if isinstance(it, dict):
            u = cast(dict[str, Any], it).get("url")
            if isinstance(u, str) and u:
                return u
    return str(a2a.get("url") or "")


def protocol_version_of(a2a: dict[str, Any]) -> str:
    """对端声明的 A2A 协议版本（v1.0 在 ``supportedInterfaces[].protocolVersion``）。"""
    for it in cast(list[Any], a2a.get("supportedInterfaces") or []):
        if isinstance(it, dict):
            v = cast(dict[str, Any], it).get("protocolVersion")
            if isinstance(v, str) and v:
                return v
    return ""


def card_from_a2a(
    a2a: dict[str, Any],
    *,
    name: str | None = None,
    cwd: str = "",
    now: float | None = None,
) -> NeighborCard:
    """标准 A2A AgentCard → NeighborCard（识别为 ``EXTERNAL_A2A_AGENT``）。

    ``name`` 可强制覆盖（用于「目录即口音」的本地别名）；否则取卡里的 name。
    ``profiles`` 只保留对端**明确声明**的 FP profile——裸 A2A 卡自然为空。
    """
    t = time.time() if now is None else now
    caps = cast(dict[str, Any], a2a.get("capabilities") or {})
    declared = [p for u, p in _URI_TO_PROFILE.items() if u in _extension_uris(caps)]
    # 任何 A2A 对端天然具备 bare-a2a（底线能力）；额外声明 fp-contract-profile 才是 FP。
    profiles = [*declared, BARE_A2A] if declared else [BARE_A2A]
    skills: list[str] = []
    for s in cast(list[Any], a2a.get("skills") or []):
        if isinstance(s, dict):
            entry = cast(dict[str, Any], s)
            sid = entry.get("id") or entry.get("name")
            if isinstance(sid, str):
                skills.append(sid)
    url = _endpoint_url(a2a)
    card = NeighborCard(
        name=name or str(a2a.get("name") or "external-a2a"),
        kind=NeighborKind.EXTERNAL_A2A_AGENT,
        cwd=cwd,
        business=str(a2a.get("description") or ""),
        tags=[],
        status=PeerStatus.IDLE,  # 外部对端的 busy 未知；发现即视为可尝试
        endpoints=[Endpoint(kind="a2a", address=url, priority=100)] if url else [],
        codec="a2a",
        capabilities=Capabilities(
            profiles=profiles,
            semantic_layers="SEMANTIC_CAPABLE",
            skills=skills,
        ),
        trust=TrustLevel.STRANGER,  # 裸外部：升级只由本地策略决定，不凭声明
        heartbeat=t,
        issued_at=t,
    )
    card.card_hash = card.compute_hash()
    return card


def card_to_a2a(card: NeighborCard) -> dict[str, Any]:
    """NeighborCard → 标准 A2A AgentCard（把本实例暴露给外部 A2A 网络时用）。"""
    url = card.endpoints[0].address if card.endpoints else ""
    return {
        "name": card.name,
        "description": card.business,
        "url": url,
        "version": "1.0",
        "capabilities": {
            "streaming": True,
            "extensions": [
                {"uri": FP_PROFILE_URI, "description": "FP zeta 契约协商 profile"},
            ],
        },
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [{"id": s, "name": s} for s in (card.capabilities.skills or [])],
    }


# ── Envelope ↔ A2A Message ───────────────────────────────


def _text_of(msg: dict[str, Any]) -> str:
    """拼接 A2A Message 的 text parts。

    入站解析**宽松**：v1.0 的 part 是扁平的 ``{"text":…}``（无 kind 字段），
    v0.x 的是 ``{"kind":"text","text":…}``——两者都认，有 ``text`` 即取。
    """
    out: list[str] = []
    for p in cast(list[Any], msg.get("parts") or []):
        if not isinstance(p, dict):
            continue
        part = cast(dict[str, Any], p)
        if part.get("text") is not None:
            out.append(str(part.get("text")))
        elif part.get("kind") == "text" or part.get("type") == "text":
            out.append(str(part.get("text") or ""))
    return "".join(out)


def envelope_to_a2a_message(env: Envelope, *, version: str = PROTOCOL_V1) -> dict[str, Any]:
    """IR 信封 → A2A Message（出站）。语义元数据放进 ``metadata``（A2A 允许扩展）。

    出站**按对端版本选形状**（见文件头差异表）：v1.0 用 ``ROLE_USER`` + 扁平
    ``{text, mediaType}`` part；v0.x 用 ``user`` + ``{kind:"text", text}``。
    """
    v1 = version.startswith("1.")
    base: dict[str, Any] = {
        "messageId": env.id,
        "contextId": env.correlation_id,
        "role": "ROLE_USER" if v1 else "user",
        "parts": (
            [{"text": env.payload.body or "", "mediaType": "text/plain"}]
            if v1
            else [{"kind": "text", "text": env.payload.body or ""}]
        ),
        "metadata": {
            "from": env.from_,
            "topic": env.payload.topic,
            "fpKind": env.kind.value,
            "idempotencyKey": env.idempotency_key,
        },
    }
    if not v1:
        base["kind"] = "message"
        base["taskId"] = env.correlation_id
    return base


def build_send_message_request(
    message: dict[str, Any],
    *,
    rpc_id: str = "1",
    version: str = PROTOCOL_V1,
) -> dict[str, Any]:
    """构造 JSON-RPC envelope（纯函数，便于单测；HTTP 由 a2a_http 搬运）。"""
    method = METHOD_SEND_MESSAGE if version.startswith("1.") else "message/send"
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": {"message": message}}


def extract_reply_text(resp: dict[str, Any]) -> str:
    """从 JSON-RPC 响应里抽取回复文本（兼容 Task.artifacts / status.message / 直接 Message）。"""
    result = cast(dict[str, Any], resp.get("result") or {})
    task = result.get("task")
    if not isinstance(task, dict):
        task = result if "status" in result else None
    if isinstance(task, dict):
        task = cast(dict[str, Any], task)
        chunks: list[str] = []
        for a in cast(list[Any], task.get("artifacts") or []):
            if isinstance(a, dict):
                chunks.append(_text_of(cast(dict[str, Any], a)))
        if chunks:
            return "".join(chunks)
        status = cast(dict[str, Any], task.get("status") or {})
        msg = status.get("message")
        if isinstance(msg, dict):
            return _text_of(cast(dict[str, Any], msg))
    msg = result.get("message")
    if isinstance(msg, dict):
        return _text_of(cast(dict[str, Any], msg))
    return ""


def a2a_message_to_envelope(
    msg: dict[str, Any],
    *,
    from_: str,
    to: str,
    now: float | None = None,
) -> Envelope:
    """A2A Message → IR 信封（入站）。

    **信任降级点在此时发生**：外部内容一律 ``UNTRUSTED_CONTENT``，无论对端在
    metadata 里如何自称——接收方的信任判断不由发送方写入。
    """
    t = time.time() if now is None else now
    text = _text_of(msg)
    md = cast(dict[str, Any], msg.get("metadata") or {})
    mid = str(msg.get("messageId") or uuid.uuid4().hex)
    return Envelope(
        id=mid,
        correlation_id=str(msg.get("contextId") or msg.get("taskId") or mid),
        from_=str(md.get("from") or from_),
        to=to,
        kind=MsgKind.SEMANTIC,
        payload=Payload(
            topic=str(md.get("topic") or "chat"),
            body=text,
            injection=InjectionHint(
                inject_as=InjectAs.USER_MESSAGE,
                trust_label=ContentTrust.UNTRUSTED_CONTENT,  # 外部来源：不信
            ),
        ),
        idempotency_key=md.get("idempotencyKey"),
        ttl=Ttl(deadline=t + _DEFAULT_TTL_SECONDS),
        sent_at=t,
    )
