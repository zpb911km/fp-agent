"""对等性与主权边界的**结构性**测试（机器守门，不靠散文）。

为什么要有这个文件
==================
协议文档写着"邻居对等"，但"对等"过去只是四条文学化的原则——没法验。于是任何
一个后来者（包括未来的我自己）都可能出于好意往邻居面加一个"顺手"的动作：
``peer_kill`` / ``peer_spawn`` / ``peer_interrupt`` / "远程续预算"。加完之后，
文档里的"对等"就成了一句空话，而且**没有任何测试会红**。

本文件把三条结构约束钉成断言：

1. **动作集封闭（角色置换不变）**：把 ``from_`` 与 ``to`` 互换，判定结论与可达分支
   必须一模一样。凡置换后被"允许/禁止"结论改变的，都不是对等的协议动作。
2. **信封没有权威字段**：wire 上不允许出现"加压/优先级/授权"类的键。权力不写进
   消息，就不会随消息流动。
3. **工具面没有控制动词**：邻居面上不许出现 kill / spawn / interrupt / grant 这类
   动词。控制属于本地人类对本地实例（主权面），不属于协议。

一句话：**kill 权来自 fork 权，不来自协议。** 谁起的谁负责；对等网络里没有中心，
也就不该有"让中心替我杀"的工具。
"""

from __future__ import annotations

import re
from pathlib import Path

from zeta_signing import make_svc, publish_self, sign_env

from fp_core.plugins.zeta import codec
from fp_core.plugins.zeta.discovery import SharedDirBackend
from fp_core.plugins.zeta.domain import ContentTrust, Envelope, MsgKind, Payload
from fp_core.plugins.zeta.peer import PeerService


def _svc(name: str, root: Path, injected: list[tuple[str, str]]) -> PeerService:
    return make_svc(name, root, lambda k, c, *, wake=False: injected.append((k, c)))


def _env(
    frm: str, to: str, *, topic: str = "contract", signed_by: str | None = None, root: Path | None = None
) -> Envelope:
    env = Envelope(
        id=f"e-{frm}-{to}-{topic}",
        correlation_id="c1",
        from_=frm,
        to=to,
        kind=MsgKind.SEMANTIC,
        payload=Payload(topic=topic, body="内容"),
        sent_at=1.0,
    )
    if signed_by and root is not None:
        sign_env(env, signed_by, root)
    return env


# ── ① 角色置换不变 ──────────────────────────────────────


def test_attribution_verdict_is_symmetric_under_role_swap(tmp_path: Path) -> None:
    """A 验 B 的信 与 B 验 A 的信，必须得到**同一种结论**（first/pinned/unsigned）。

    若哪天有人在收件侧加了单向门禁（例如"只信比自己先注册的实例"），这条会红。
    """
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)
    # 双方都发名片（生产行为），所以互相都是"首见"
    publish_self("A", tmp_path)
    publish_self("B", tmp_path)

    env_ab = _env("A", "B", signed_by="A", root=tmp_path)
    env_ba = _env("B", "A", signed_by="B", root=tmp_path)

    verdicts = {}
    for svc, env, who in ((b, env_ab, "A→B"), (a, env_ba, "B→A")):
        trust, status = svc.attribute(env)
        verdicts[who] = (trust, status)

    assert verdicts["A→B"] == verdicts["B→A"] == (ContentTrust.TRUSTED_PEER, "first")


def test_unsigned_verdict_is_symmetric_under_role_swap(tmp_path: Path) -> None:
    """未签名同样对称：无论谁发的，都降到 UNTRUSTED + unsigned。"""
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)
    publish_self("A", tmp_path)
    publish_self("B", tmp_path)

    got = {
        "A→B": b.attribute(_env("A", "B")),
        "B→A": a.attribute(_env("B", "A")),
    }
    assert got["A→B"] == got["B→A"] == (ContentTrust.UNTRUSTED_CONTENT, "unsigned")


def test_chat_gate_is_symmetric(tmp_path: Path) -> None:
    """同一主题在**两个方向**上被同样放行/拦下（门禁不能只朝一个方向开）。"""
    injected: list[tuple[str, str]] = []
    a = _svc("A", tmp_path, injected)
    b = _svc("B", tmp_path, injected)
    publish_self("A", tmp_path)
    publish_self("B", tmp_path)

    for topic in ("chat", "contract"):
        env_ab = _env("A", "B", topic=topic)  # 未签名
        env_ba = _env("B", "A", topic=topic)
        codec.encode(env_ab), codec.encode(env_ba)  # 同一编码路径

        from fp_core.plugins.zeta.transport import DirTransport

        # 未签名 chat 放行、未签名 contract 拒收——两个方向一致
        DirTransport(tmp_path).deliver("B", codec.encode(env_ab))
        DirTransport(tmp_path).deliver("A", codec.encode(env_ba))
        n_ab, n_ba = b.poll(), a.poll()
        assert n_ab == n_ba, f"主题 {topic} 在两个方向上结论不同（{n_ab} vs {n_ba}）"


def test_contract_negotiation_works_in_both_directions(tmp_path: Path) -> None:
    """契约协商没有"发起方特权"：A→B 与 B→A 都能走完机器校验（双锁不认人）。"""
    from fp_core.plugins.zeta.contracts import ContractExchange

    injected: list[tuple[str, str]] = []
    ex = ContractExchange(tmp_path / "contracts")
    a = make_svc("A", tmp_path, lambda k, c, *, wake=False: injected.append((k, c)), exchange=ex)
    b = make_svc("B", tmp_path, lambda k, c, *, wake=False: injected.append((k, c)), exchange=ex)

    contract = {
        "contract_version": "1.0",
        "id": "ctr-parity",
        "name": "parity",
        "revision": 1,
        "status": "proposed",
        "parties": [{"name": "A", "role": "backend"}, {"name": "B", "role": "frontend"}],
        "scope": {"resources": {"default_effect": "deny"}},
    }
    # A 提议 → B 收到并校验（不 ack，只验"能不能发起"）
    a.propose_contract("B", contract)
    assert b.poll() == 1
    assert "机器校验" in b.answer()

    # 反向：B 提议 → A 收到并校验。发起方身份不改变任何校验规则。
    b.propose_contract("A", contract)
    assert a.poll() == 1
    assert "机器校验" in a.answer()


# ── ② 信封没有权威字段 ──────────────────────────────────


WIRE_KEYS = {
    "id",
    "correlation_id",
    "from",
    "to",
    "kind",
    "reply_to",
    "idempotency_key",
    "ttl",
    "sent_at",
    "payload",
    "codec_meta",
    "sig",
    "hops",
}

# 出现即意味着"消息里携带权力"——这类键必须走本地路径，不许上 wire
FORBIDDEN_KEY_HINTS = re.compile(
    r"(force|prio|authorit|admin|grant|lease|quota|budget|kill|spawn|sudo|root|trust_level)"
)


def test_envelope_wire_has_no_authority_fields(tmp_path: Path) -> None:
    """wire 键集固定且不含权威字段：权力不写进消息，就不会随消息流动。

    若有人想加 ``"priority": 9`` 或 ``"authorized": true`` 之类的字段来"让某些来信
    优先"——请先回答：**凭什么**？凭一封信自称吗？那就不叫权限，叫自我加冕。
    """
    wire = codec.envelope_to_wire(_env("A", "B"))
    assert set(wire) == WIRE_KEYS
    assert not [k for k in wire if FORBIDDEN_KEY_HINTS.search(k)]
    # payload 内部也不许出现权威键（否则改个位置就绕过了）
    payload = codec.envelope_to_wire(_env("A", "B"))["payload"]
    assert not [k for k in payload if FORBIDDEN_KEY_HINTS.search(k)], payload


def test_no_control_verbs_on_the_peer_surface() -> None:
    """工具面不许出现控制动词：邻居面只有"说/听/协商"，没有"杀/起/打断"。

    控制属于**主权面**（本地人类 ↔ 本地实例）：kill 权来自 fork 权，不来自协议。
    想做"能杀邻居"的功能，正确做法是自己起它（host 面），而不是给协议加一个后门。
    """
    import inspect

    import fp_core.plugins.zeta.plugin as zplugin

    names = set(re.findall(r'\("(peer_[a-z_]+|alarm)",\s*[A-Z_]+_TOOL', inspect.getsource(zplugin)))
    assert names == {"peer_list", "peer_send", "peer_answer", "peer_propose", "peer_ack", "alarm"}, names

    forbidden = ("kill", "spawn", "interrupt", "stop", "restart", "grant", "revoke", "budget", "lease", "quota")
    for tool_name in names:
        assert not [w for w in forbidden if w in tool_name], f"邻居面出现了控制类工具名：{tool_name}"


def test_peer_tool_params_carry_no_authority() -> None:
    """工具**参数**同样不许有"加压/授权"类的开关（换汤不换药的后门）。"""
    import fp_core.plugins.zeta.plugin as zplugin

    for const in ("PEER_SEND_TOOL", "PEER_PROPOSE_TOOL", "PEER_ACK_TOOL", "PEER_ANSWER_TOOL", "PEER_LIST_TOOL"):
        tool = getattr(zplugin, const)
        props = set(tool["function"]["parameters"].get("properties") or {})
        assert not [k for k in props if FORBIDDEN_KEY_HINTS.search(k)], f"{const} 参数里出现权威字段：{props}"


# ── ③ 收件箱归属：同名不同钥不许"合并" ─────────────────


def test_two_instances_with_same_name_never_share_a_key(tmp_path: Path) -> None:
    """同名实例各持独立私钥（名字是**地址**，不是身份）。签名必须来自地址背后的那把钥。"""
    from fp_core.plugins.zeta.discovery import build_card
    from fp_core.plugins.zeta.identity import Identity

    one = Identity.load_or_create("dup", tmp_path / "s1")
    two = Identity.load_or_create("dup", tmp_path / "s2")
    assert one is not None and two is not None
    assert one.pub != two.pub

    # 用 s1 的钥签名、以"dup"之名投给第三方：第三方 pin 的是 s1 的公钥，
    # 于是 s2 用自己的钥冒充同名会被判 changed——名字相同不构成信任传递。
    injected: list[tuple[str, str]] = []
    b = _svc("B", tmp_path, injected)
    SharedDirBackend(tmp_path).publish(
        build_card(name="dup", cwd=str(tmp_path), business="x", identity=one.card_identity())
    )
    env = _env("dup", "B")
    sign_env(env, "dup", tmp_path)  # 实际用的是 <tmp>/zeta-state 下那把（与 s1/s2 都不同）
    b.attribute(env)  # 首见 → pin 到这一把

    env2 = _env("dup", "B", topic="chat")
    env2.id = "e2"
    env2.sig = two.sign(codec.signing_payload(env2))  # 换一把钥
    trust, status = b.attribute(env2)
    assert status in ("sig_fail", "key_changed")
    assert trust is ContentTrust.UNTRUSTED_CONTENT
