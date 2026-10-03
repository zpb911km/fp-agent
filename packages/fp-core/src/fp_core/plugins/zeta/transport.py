"""Zeta 传输层 —— 消息怎么送达（可插拔）。

MVP：**DirTransport** —— 同机文件收件箱。天然支持离线留话：对方不在线，
消息文件仍写进其 inbox，上线轮询时读到（at-least-once；幂等由 idempotency_key 管）。

跨机：换 frp/tcp 实现（P3），领域模型不感知差别。

TODO(n12) 跨网络传输后端（已存档暂缓）：公网互联需要一个「会合点」（控制面），但不一定
需要中转（数据面）——打洞可让服务器只当接线员。推荐优先接 Tailscale/WireGuard（打洞为主、
DERP 中继兜底、对应用透明），frp 纯中继兜底。传输层可插拔，接入时领域模型不改。
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from fp_core.plugins.zeta.discovery import default_peers_dir


def _safe(name: str) -> str:
    safe = "".join(c for c in name if c.isalnum() or c in "-_.")
    if not safe:
        raise ValueError(f"非法邻居名: {name!r}")
    return safe


class DirTransport:
    """``<root>/<peer>.inbox/msg-<id>.json`` —— 文件即消息。"""

    def __init__(self, root: Path | None = None) -> None:
        self.root = Path(root) if root is not None else default_peers_dir()

    def inbox_dir(self, name: str) -> Path:
        return self.root / f"{_safe(name)}.inbox"

    def deliver(self, to: str, text: str) -> str:
        """投递一条消息（原子写）。返回 msg_id。对方离线也成立。"""
        d = self.inbox_dir(to)
        d.mkdir(parents=True, exist_ok=True)
        msg_id = uuid.uuid4().hex
        target = d / f"msg-{msg_id}.json"
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, target)
        return msg_id

    def drain(self, name: str) -> list[str]:
        """取走自己 inbox 里全部待处理消息（消费即移入 .processed/）。"""
        d = self.inbox_dir(name)
        if not d.exists():
            return []
        done = d / ".processed"
        done.mkdir(exist_ok=True)
        out: list[str] = []
        for f in sorted(d.glob("msg-*.json")):
            try:
                text = f.read_text(encoding="utf-8")
            except OSError:
                continue
            os.replace(f, done / f.name)
            out.append(text)
        return out

    def has_pending(self, name: str) -> bool:
        d = self.inbox_dir(name)
        return d.exists() and any(d.glob("msg-*.json"))

    def purge_processed(self, name: str, max_age: float = 7 * 86400.0, *, now: float | None = None) -> int:
        """清理 ``.processed/`` 中超过 ``max_age`` 的消息文件（存储卫生）。

        消费即移入 ``.processed/``，长期会累积；此处按 mtime 回收过期历史。
        返回删除的文件数。
        """
        done = self.inbox_dir(name) / ".processed"
        if not done.is_dir():
            return 0
        t = time.time() if now is None else now
        n = 0
        for f in done.glob("msg-*.json"):
            try:
                if t - f.stat().st_mtime > max_age:
                    f.unlink()
                    n += 1
            except OSError:
                continue
        return n
