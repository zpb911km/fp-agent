"""邻居面的预算硬闸（P0）—— 谁能消耗我多少资源，由我本地说了算。

为什么必须有
============
修复前，邻居面唯一的"限流"是 ``ttl``（太旧的留话不打扰）与幂等去重（重投不重复
响铃）。两者都不限制**量**：一个邻居（或一个写收件箱的脚本）可以投 10 万条消息，
每条都变成一次铃声注入、一次上下文占用、一次 LLM 调用。这不是安全问题而是
**可用性问题**，且它不需要攻击者——一次失控的重投循环就够了。

三条硬闸
========
1. **单条上限**：信封字节数超限直接拒收（``max_envelope_bytes``），正文过长
   截断（见 peer.py）。防止单条消息吃掉窗口。
2. **滑动窗口配额**：每邻居 + 全实例两级，各限条数与字节数。超了就不再注入
   （消息留在 ``.processed/``，可人工翻查），并留一条审计——**沉默丢弃是不可接受的**。
3. **未接来电堆积上限**：``max_pending`` 满时不再响铃（否则上下文被铃声刷爆）。

远程不可续期（设计约束，不是功能缺失）
======================================
本模块**没有任何**"加额度"的远程入口。唯一的调整路径是本地环境变量
``FP_ZETA_BUDGET``（人写的），生效需要重启实例（人的动作）。这不是偷懒：

    如果远程能给我续预算，它就能用"资源"换"服从"——预算面就成了不对等的
    控制通道，而对等网络里不允许存在单向控制（见 docs/dev/zeta_协议.md
    「主权三分」：预算只能本地人类签发、本地人类续期）。

计数器是滑动窗口而非"总额度"：窗口自然滚动，不需要谁去重置。**滚动不等于
续期**——滚动的是"最近一小时"的度量，续期是"提高上限"，后者没有任何远程通道。

与远程 run 的关系（设计拒绝）
============================
当前邻居能造成的消耗只有"注入"这一条通道——没有远程 run 执行器，而且这是**刻意
的**：协议已明确拒绝该设计（docs/dev/zeta_协议.md §11.4）。时长 / 工具调用数 /
文件写入量三项因此**不打算有**消费者，不是"等着实现"。

保留"可扩展的计量器"形状只是**形状纪律**，不构成待办：若哪天 §11.4 的四条门槛
（默认关 / 只认已验签 / 预算本地签发 / 我能掐死它）全过，按此形状加三个计数器
即可，不必另造一套。

**别混淆**：本闸门限的是**注入通道**，不等于远程 run 已有资源隔离——后者是零。
"""

from __future__ import annotations

import json
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, cast

from fp_core.logger import get_logger

logger = get_logger()


@dataclass(frozen=True)
class Quota:
    """滑动窗口配额：``window`` 秒内最多 ``msgs`` 条、``nbytes`` 字节。"""

    msgs: int
    nbytes: int
    window: float = 3600.0

    def scaled(self, factor: float) -> Quota:
        return Quota(max(1, int(self.msgs * factor)), max(1024, int(self.nbytes * factor)), self.window)


# 全实例：一小时最多 120 条 / 1 MiB（≈ 每分钟 2 条、每条约 8 KiB）
DEFAULT_INSTANCE_QUOTA = Quota(msgs=120, nbytes=1 << 20)
# 单邻居：一小时最多 40 条 / 256 KiB（一小时内没有任何一件事需要邻居投 40 封信）
DEFAULT_PEER_QUOTA = Quota(msgs=40, nbytes=256 << 10)
# 未接来电堆积上限（每通都占一个铃铛 + 一份正文）
DEFAULT_MAX_PENDING = 32


def _quota_from(d: dict[str, Any] | None, base: Quota) -> Quota:
    if not isinstance(d, dict):
        return base
    try:
        return Quota(
            msgs=int(d.get("msgs", base.msgs)),
            nbytes=int(d.get("bytes", d.get("nbytes", base.nbytes))),
            window=float(d.get("window", base.window)),
        )
    except (TypeError, ValueError):
        return base


class BudgetGate:
    """两级滑动窗口计量器。``charge`` 返回 None = 放行，否则返回拒收原因。"""

    def __init__(
        self,
        *,
        instance: Quota | None = None,
        per_peer: Quota | None = None,
        max_pending: int = DEFAULT_MAX_PENDING,
    ) -> None:
        self.instance = instance or DEFAULT_INSTANCE_QUOTA
        self.per_peer = per_peer or DEFAULT_PEER_QUOTA
        self.max_pending = max_pending
        self._buckets: dict[str, deque[tuple[float, int]]] = {}

    # ── 构造 ──

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> BudgetGate:
        """从 ``FP_ZETA_BUDGET`` 读配置（JSON）；坏配置一律回落到默认值并告警。"""
        environ = env if env is not None else dict(os.environ)
        raw = environ.get("FP_ZETA_BUDGET")
        if not raw:
            return cls()
        try:
            parsed = json.loads(raw)
        except (ValueError, TypeError):
            logger.warning("[zeta] FP_ZETA_BUDGET 不是合法 JSON，使用默认预算")
            return cls()
        if not isinstance(parsed, dict):
            return cls()
        conf = cast("dict[str, Any]", parsed)
        return cls(
            instance=_quota_from(cast("dict[str, Any] | None", conf.get("instance")), DEFAULT_INSTANCE_QUOTA),
            per_peer=_quota_from(cast("dict[str, Any] | None", conf.get("peer")), DEFAULT_PEER_QUOTA),
            max_pending=int(conf.get("max_pending", DEFAULT_MAX_PENDING)),
        )

    # ── 计量 ──

    @staticmethod
    def _prune(bucket: deque[tuple[float, int]], now: float, window: float) -> None:
        while bucket and now - bucket[0][0] >= window:
            bucket.popleft()

    @staticmethod
    def _totals(bucket: deque[tuple[float, int]]) -> tuple[int, int]:
        return len(bucket), sum(b[1] for b in bucket)

    def check(self, source: str, nbytes: int, *, now: float | None = None) -> str | None:
        """只看不记账（用于预检/自检）；返回拒收原因或 None。"""
        t = time.time() if now is None else now
        for key, quota in (("_instance", self.instance), (source, self.per_peer)):
            bucket = self._buckets.get(key)
            if not bucket:
                continue
            self._prune(bucket, t, quota.window)
            n, size = self._totals(bucket)
            if n + 1 > quota.msgs:
                return f"{'instance' if key == '_instance' else 'peer'}_msgs"
            if size + nbytes > quota.nbytes:
                return f"{'instance' if key == '_instance' else 'peer'}_bytes"
        return None

    def charge(self, source: str, nbytes: int, *, now: float | None = None) -> str | None:
        """记账并判定。放行返回 None；超限返回原因（**不记账**，避免超额条目互相驱逐）。"""
        reason = self.check(source, nbytes, now=now)
        if reason is not None:
            return reason
        t = time.time() if now is None else now
        for key in ("_instance", source):
            self._buckets.setdefault(key, deque()).append((t, nbytes))
        return None

    def pending_full(self, pending: int) -> bool:
        return pending >= self.max_pending

    def usage(self, source: str, *, now: float | None = None) -> dict[str, int]:
        """当前窗口用量（给自己看/给测试看；不是远程接口）。"""
        t = time.time() if now is None else now
        out: dict[str, int] = {}
        for key, quota in (("instance", self.instance), (f"peer:{source}", self.per_peer)):
            bucket = self._buckets.get("_instance" if key == "instance" else source)
            if not bucket:
                out[f"{key}.msgs"] = 0
                out[f"{key}.bytes"] = 0
                continue
            self._prune(bucket, t, quota.window)
            n, size = self._totals(bucket)
            out[f"{key}.msgs"] = n
            out[f"{key}.bytes"] = size
        return out
