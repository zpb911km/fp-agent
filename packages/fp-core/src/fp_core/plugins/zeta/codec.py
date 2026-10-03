"""Zeta codec 层 —— IR ↔ wire 映射（可插拔）。

MVP 提供 **dir_codec**：同机文件传输用的直接 JSON 编码。
P3 提供 **a2a_codec**：映射到 A2A JSON-RPC（HTTP/SSE），用于异构互联。

本层是唯一允许触碰「协议/传输细节」的地方；领域模型保持协议无关
（删掉 codec 后领域层测试仍全绿）。
"""

from __future__ import annotations

import json
from typing import Any, cast

from fp_core.plugins.zeta.domain import (
    ContentTrust,
    Envelope,
    InjectAs,
    InjectionHint,
    MsgKind,
    Part,
    Payload,
    Ttl,
    canonical_json,
)

# ── Ttl ──


def ttl_to_wire(t: Ttl) -> dict[str, Any]:
    return {"deadline": t.deadline, "max_hops": t.max_hops}


def ttl_from_wire(d: dict[str, Any]) -> Ttl:
    dl = d.get("deadline")
    return Ttl(deadline=float(dl) if dl is not None else None, max_hops=int(d.get("max_hops", 1)))


# ── Part ──


def part_to_wire(p: Part) -> dict[str, Any]:
    return {"type": p.type, "text": p.text, "uri": p.uri, "data": p.data}


def part_from_wire(d: dict[str, Any]) -> Part:
    return Part(
        type=str(d.get("type") or "text"),
        text=d.get("text"),
        uri=d.get("uri"),
        data=cast("dict[str, Any] | None", d.get("data")),
    )


# ── InjectionHint ──


def injection_to_wire(i: InjectionHint) -> dict[str, Any]:
    return {
        "inject_as": i.inject_as.value,
        "trust_label": i.trust_label.value,
        "max_context_tokens": i.max_context_tokens,
    }


def injection_from_wire(d: dict[str, Any]) -> InjectionHint:
    tok = d.get("max_context_tokens")
    return InjectionHint(
        inject_as=InjectAs(d.get("inject_as", InjectAs.USER_MESSAGE.value)),
        trust_label=ContentTrust(d.get("trust_label", ContentTrust.UNTRUSTED_CONTENT.value)),
        max_context_tokens=int(tok) if tok is not None else None,
    )


# ── Payload ──


def payload_to_wire(p: Payload) -> dict[str, Any]:
    return {
        "kind_mech": p.kind_mech,
        "topic": p.topic,
        "body": p.body,
        "parts": [part_to_wire(x) for x in p.parts] if p.parts else None,
        "injection": injection_to_wire(p.injection) if p.injection else None,
    }


def payload_from_wire(d: dict[str, Any]) -> Payload:
    parts_raw = cast("list[Any] | None", d.get("parts"))
    inj_raw = d.get("injection")
    return Payload(
        kind_mech=d.get("kind_mech"),
        topic=d.get("topic"),
        body=d.get("body"),
        parts=[part_from_wire(cast("dict[str, Any]", x)) for x in parts_raw] if parts_raw else None,
        injection=injection_from_wire(cast("dict[str, Any]", inj_raw)) if inj_raw else None,
    )


# ── Envelope ──


def envelope_to_wire(env: Envelope) -> dict[str, Any]:
    """IR → wire（``from_`` 在 wire 上写回 ``from``）。"""
    return {
        "id": env.id,
        "correlation_id": env.correlation_id,
        "from": env.from_,
        "to": env.to,
        "kind": env.kind.value,
        "reply_to": env.reply_to,
        "idempotency_key": env.idempotency_key,
        "ttl": ttl_to_wire(env.ttl),
        "sent_at": env.sent_at,
        "payload": payload_to_wire(env.payload),
        "codec_meta": env.codec_meta,
        "sig": env.sig,
    }


def envelope_from_wire(d: dict[str, Any]) -> Envelope:
    return Envelope(
        id=str(d["id"]),
        correlation_id=str(d.get("correlation_id") or d["id"]),
        from_=str(d.get("from") or d.get("from_") or "?"),
        to=str(d["to"]),
        kind=MsgKind(d.get("kind", MsgKind.SEMANTIC.value)),
        payload=payload_from_wire(cast("dict[str, Any]", d.get("payload") or {})),
        reply_to=d.get("reply_to"),
        idempotency_key=d.get("idempotency_key"),
        ttl=ttl_from_wire(cast("dict[str, Any]", d.get("ttl") or {})),
        sent_at=float(d.get("sent_at") or 0.0),
        codec_meta=cast("dict[str, Any] | None", d.get("codec_meta")),
        sig=d.get("sig"),
    )


def encode(env: Envelope) -> str:
    """dir_codec：直接 JSON 规范化编码。"""
    return canonical_json(envelope_to_wire(env))


def decode(text: str) -> Envelope:
    return envelope_from_wire(cast("dict[str, Any]", json.loads(text)))
