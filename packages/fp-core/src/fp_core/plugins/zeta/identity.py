"""实例身份与信封归属（P0 安全闸门）。

问题（修复前）
==============
同机收件箱就是**文件**：``<peers>/<name>.inbox/msg-*.json``。信封里的 ``from_``
由发件方自填，接收方此前**不做任何校验**——任何能写进该目录的进程都能以任意
自称的身份，往对方上下文里注入一条 ``user`` 消息。``require`` 判"回了没有"
所读的 ``last_inbound_from`` 也是同一个自填字段，一样可被伪造。

我们能修到什么程度（诚实边界）
==============================
同一 uid 能写收件箱，就也能读 ``keys/<name>.key``（0600）——**同 uid 的主动
攻击者不可能被本地密码学挡住**。所以本模块的目标不是"隔离"，是**归因**：

- 修前：任何人都能静默冒充任何名字，连痕迹都没有。
- 修后：冒充需要**读取**某个实例的私钥文件（一项留下痕迹的故意行为）；而
  偶发误传（名字解析错、``to`` 打错、第三方脚本自称 ``peer:x``）不再可能被当真。
- 跨机（P3 frp / A2A 外部对端）时，非对称签名是唯一可行的归属手段——本模块
  就是为那一层铺的路，现在做掉可以避免将来换协议时重写接收端。

设计
====
- 每实例一对 Ed25519 密钥：``<zeta_state>/keys/<name>.key``（0600，目录 0700，
  首次启动本地生成；私钥不出本机、不落日志、不经任何网络）。
- 名片携带公钥：``identity = {alg, kid, pub}``；``kid = sha256(pub)[:16]``。
- 签名串：``ed25519:<kid>:<b64url>``，覆盖 ``canonical_json(信封去掉 sig)``
  ——签名自描述（alg 与 kid 都在串里），wire 上只多一个字段，旧信封无需迁移。
- 收件：按**发件人名片**取公钥验签；结论经 TOFU pinning（``trust.json``）收敛
  为三态 ``pinned`` / ``first`` / ``changed``。
  ``changed``（同名公钥变了）→ 一律降 UNTRUSTED 并告警：改名换钥该换名字，
  否则就是可疑事件（SSH known_hosts 语义）。**不做自动重新 pin** —— 自动重 pin
  等于把 TOFU 降级成"每次都信"，那就不如不做。

为什么不宣称"安全"
==================
名片本身也写在同一个共享目录里，攻击者可以连名片一起改写。TOFU 的价值正在
这里：它把"改名片 + 冒充"从**不可观测**变成**可观测**（首见即 pin，之后任何
改动都是一条事件）。真正的信任根不在文件里，而在"第一次见到时的那一次本地记录"。
"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from fp_core.logger import get_logger
from fp_core.plugins.zeta.discovery import zeta_state_dir

logger = get_logger()

KEY_FILE_HEADER = "fp-zeta-ed25519-v1"
SIG_ALG = "ed25519"


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def kid_of(pub_b64: str) -> str:
    """密钥身份指纹：sha256(pub) 前 16 hex。用于"同名是否同钥"的快速判定。"""
    return hashlib.sha256(pub_b64.encode("ascii")).hexdigest()[:16]


# ─────────────────────────────────────────────────────────
#  本实例身份
# ─────────────────────────────────────────────────────────


@dataclass
class Identity:
    """本实例的签名身份（私钥只在内存与 0600 文件里）。"""

    name: str
    kid: str
    pub: str
    _priv: Any = None  # Ed25519PrivateKey（不参与 repr）

    # ── 构造 ──

    @staticmethod
    def keys_dir(root: Path | None = None) -> Path:
        return (root or zeta_state_dir()) / "keys"

    @classmethod
    def load_or_create(cls, name: str, root: Path | None = None) -> Identity | None:
        """读取或生成本实例密钥。缺 ``cryptography`` 时返回 None（调用方降级）。

        **返回 None 而不是抛异常**：密码学后端缺失不该让整个 zeta 崩掉——后果
        应该是"归属能力消失 → 一律按 UNTRUSTED 收"，而不是"邻居面打不开"。
        """
        try:
            from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
            from cryptography.hazmat.primitives.serialization import (
                Encoding,
                PublicFormat,
            )
        except ImportError:
            logger.warning("[zeta] 缺 cryptography：信封归属不可用，来信一律按 UNTRUSTED 处理")
            return None

        safe = "".join(c for c in name if c.isalnum() or c in "-_.") or "default"
        d = cls.keys_dir(root)
        path = d / f"{safe}.key"
        priv = None
        if path.exists():
            try:
                lines = path.read_text(encoding="utf-8").splitlines()
                if len(lines) >= 2 and lines[0].strip() == KEY_FILE_HEADER:
                    priv = Ed25519PrivateKey.from_private_bytes(bytes.fromhex(lines[1].strip()))
            except (OSError, ValueError) as exc:
                logger.warning(f"[zeta] 私钥不可读（{path}）：{exc} → 重新生成（旧签名来信将判 UNTRUSTED）")
                priv = None
        if priv is None:
            priv = Ed25519PrivateKey.generate()
            try:
                d.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(path.name + ".tmp")
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(f"{KEY_FILE_HEADER}\n{priv.private_bytes_raw().hex()}\n")
                os.replace(tmp, path)
                _chmod(path, 0o600)
            except OSError as exc:
                logger.warning(f"[zeta] 私钥落盘失败（{path}）：{exc} —— 本次运行仅内存可用")
        raw_pub = priv.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
        pub = _b64e(raw_pub)
        return cls(name=name, kid=kid_of(pub), pub=pub, _priv=priv)

    # ── 使用 ──

    def available(self) -> bool:
        return self._priv is not None

    def card_identity(self) -> dict[str, str]:
        """名片里发布的身份三元组。只发公钥。"""
        return {"alg": SIG_ALG, "kid": self.kid, "pub": self.pub}

    def sign(self, data: bytes) -> str | None:
        """返回 ``ed25519:<kid>:<b64>``；无密钥后端时 None（调用方发不签名的信）。"""
        if self._priv is None:
            return None
        raw = self._priv.sign(data)
        return f"{SIG_ALG}:{self.kid}:{_b64e(raw)}"


# ─────────────────────────────────────────────────────────
#  验签（收件方）
# ─────────────────────────────────────────────────────────


def parse_sig(sig: str | None) -> tuple[str, bytes] | None:
    """拆解签名字符串 → ``(kid, raw_sig)``；格式不符返回 None。"""
    if not sig:
        return None
    parts = sig.split(":")
    if len(parts) != 3 or parts[0] != SIG_ALG:
        return None
    kid, b64 = parts[1], parts[2]
    if not kid or not b64:
        return None
    try:
        return kid, _b64d(b64)
    except (ValueError, TypeError):
        return None


def verify_sig(pub_b64: str | None, sig: str | None, data: bytes) -> bool:
    """用公钥验签。任何异常都算"不通过"（验签失败绝不能抛出打断收件循环）。"""
    if not pub_b64 or not sig:
        return False
    parsed = parse_sig(sig)
    if parsed is None:
        return False
    kid, raw_sig = parsed
    if kid != kid_of(pub_b64):
        return False  # kid 与名片公钥不符：签名是给另一把钥匙的
    try:
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

        key = Ed25519PublicKey.from_public_bytes(_b64d(pub_b64))
        key.verify(raw_sig, data)
        return True
    except Exception:  # noqa: BLE001 — 含 InvalidSignature / ValueError / ImportError
        return False


# ─────────────────────────────────────────────────────────
#  TOFU pinning（信任根）
# ─────────────────────────────────────────────────────────


@dataclass
class TrustVerdict:
    """一次归属判定。``status`` ∈ pinned / first / changed / unsigned。"""

    status: str
    trust_pub: str | None = None  # 判定所依据的公钥（验签用同一把，避免"验一把信另一把"）

    @property
    def ok(self) -> bool:
        """是否可作为 TRUSTED_PEER 处理。``first`` 也算：首见即信任起点（同 SSH）。"""
        return self.status in ("pinned", "first")


class TrustStore:
    """``<zeta_state>/trust.json``：``name → {kid, pub, first_seen}``。

    **只由接收方写**（记录自己见到的对方公钥），对方改不了——这是本地状态，
    不在共享目录里，所以它能当信任根。
    """

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or (zeta_state_dir() / "trust.json")
        self._data: dict[str, dict[str, Any]] = {}
        self._load()

    def _load(self) -> None:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
            if isinstance(raw, dict):
                self._data = cast(dict[str, dict[str, Any]], raw)
        except (OSError, json.JSONDecodeError, ValueError):
            self._data = {}

    def _save(self) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.path.with_name(self.path.name + ".tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=1, sort_keys=True), encoding="utf-8")
            os.replace(tmp, self.path)
        except OSError as exc:
            logger.warning(f"[zeta] 信任库落盘失败：{exc}")

    def verdict(self, name: str, kid: str | None, pub: str | None) -> TrustVerdict:
        """判定某名字当前公钥的可信状态；``first`` 会**就地 pin**。"""
        if not kid or not pub:
            return TrustVerdict("unsigned", None)
        rec = self._data.get(name)
        if rec is None:
            self._data[name] = {"kid": kid, "pub": pub, "first_seen": time.time()}
            self._save()
            return TrustVerdict("first", pub)
        if rec.get("kid") == kid and rec.get("pub") == pub:
            return TrustVerdict("pinned", pub)
        return TrustVerdict("changed", pub)

    def pinned_pub(self, name: str) -> str | None:
        rec = self._data.get(name)
        return str(rec["pub"]) if rec and rec.get("pub") else None

    def repin(self, name: str) -> bool:
        """人工重新 pin（本地动作，无远程通道）。返回是否确有变化。"""
        rec = self._data.get(name)
        if rec is None:
            return False
        self._data.pop(name, None)
        self._save()
        return True


# ─────────────────────────────────────────────────────────
#  文件权限小工具
# ─────────────────────────────────────────────────────────


def _chmod(path: Path, mode: int) -> None:
    with contextlib.suppress(OSError):
        os.chmod(path, mode)


def ensure_private_dir(path: Path, mode: int = 0o700) -> bool:
    """把目录收紧到 ``mode``。返回是否**曾经过宽**（供调用方告警一次）。

    发现目录过去是 775（同组可写）——同组可写意味着同组任何人能往任何实例的
    收件箱投信。收件箱是注入面，必须是私有的。
    """
    try:
        path.mkdir(parents=True, exist_ok=True)
        cur = path.stat().st_mode & 0o777
    except OSError:
        return False
    if cur & 0o077:
        _chmod(path, mode)
        return True
    return False
