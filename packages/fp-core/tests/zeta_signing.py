"""测试装配：让测试实例**真的持有密钥并互发带公钥的名片**。

为什么需要
==========
P0 之后，收件路径会验签：一封没有签名（或签名所用公钥不在发件人名片里）的
``contract`` 类信件会被拒收。若测试仍用「手搭 PeerService、不发布名片、不签名」
的旧搭法，它测的是一条**生产里不存在**的路径（生产实例启动时必按名建钥、必发卡）。

所以这里把「实例该有的一切」收成一处：``<tmp>/zeta-state`` 下建钥、发卡、
信任库与审计也落进 tmp（不污染真实 ``~/.local/share/fp``）。
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

from fp_core.plugins.zeta.budget import BudgetGate
from fp_core.plugins.zeta.discovery import SharedDirBackend, build_card
from fp_core.plugins.zeta.identity import Identity, TrustStore
from fp_core.plugins.zeta.peer import PeerService
from fp_core.plugins.zeta.transport import DirTransport

InjectFn = Callable[..., None]


def state_dir(root: Path) -> Path:
    return root / "zeta-state"


def identity_for(name: str, root: Path) -> Identity:
    ident = Identity.load_or_create(name, state_dir(root))
    assert ident is not None, "测试环境缺 cryptography，无法签名"
    return ident


def publish_self(name: str, root: Path, *, business: str = "test") -> Identity:
    """按名字建/读密钥，并把带公钥的名片发进共享目录（= 生产启动时的行为）。"""
    ident = identity_for(name, root)
    card = build_card(name=name, cwd=str(root), business=business, identity=ident.card_identity())
    SharedDirBackend(root).publish(card)
    return ident


def make_svc(
    name: str,
    root: Path,
    inject: InjectFn,
    *,
    exchange: Any = None,
    trust: TrustStore | None = None,
    budget: BudgetGate | None = None,
    publish: bool = True,
    **kwargs: Any,
) -> PeerService:
    ident = identity_for(name, root)
    if publish:
        publish_self(name, root)
    return PeerService(
        name=name,
        backend=SharedDirBackend(root),
        transport=DirTransport(root),
        inject=inject,
        exchange=exchange,
        identity=ident,
        trust=trust if trust is not None else TrustStore(state_dir(root) / "trust.json"),
        budget=budget if budget is not None else BudgetGate(),
        audit_root=state_dir(root),
        **kwargs,
    )


def sign_env(env: Any, name: str, root: Path) -> Any:
    """给**手搭的信封**盖章，返回同一对象（便于内联）。

    为什么要这个
    ============
    生产里每封信都出自 ``PeerService.send``，必盖章。测试若手搭 ``Envelope`` 直接
    ``deliver``，它要么走"未验证"旁路（topic 限于 chat），要么被拒收——**测的都不是
    主路径**。凡是想验证"正常协作行为"的用例，都该先用本函数把信做成"真信"。

    想专门验证"未签名/验签失败"的行为时，**不要**调用它——那正是被测对象。
    """
    from fp_core.plugins.zeta import codec

    env.sig = identity_for(name, root).sign(codec.signing_payload(env))
    return env
