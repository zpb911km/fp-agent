"""邻居面的可审计痕迹 —— 收件拒绝、归属变更、预算耗尽都留一行。

为什么单独成模块
================
修复前，``poll()`` 对"解不开的信封"是静默 ``continue``：丢一件坏事和丢一件垃圾
在日志上无法区分。安全事件的价值不在"当时拦住"，在**事后能回答"谁试过、我拦了没有"**。
所以每条拒收都写结构化一行：时间 / 事件 / 来源 / 原因 / 关键字段摘要。

写在哪
======
``<zeta_state>/audit.jsonl`` —— **本地状态目录**，不在共享 peers 目录里（否则
攻击者可改可删，审计就成了摆设）。超过 ``MAX_BYTES`` 时滚成 ``audit.jsonl.1``
（只留一代：审计是线索，不是账本）。

失败策略
========
审计写不进去**绝不打断收件循环**：宁可少一行日志，不可因为磁盘只读就让邻居面瘫痪。
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any

from fp_core.logger import get_logger
from fp_core.plugins.zeta.discovery import zeta_state_dir

logger = get_logger()

MAX_BYTES = 2 << 20  # 2 MiB 后滚一代


def audit_path(root: Path | None = None) -> Path:
    return (root or zeta_state_dir()) / "audit.jsonl"


def record(event: str, *, root: Path | None = None, **fields: Any) -> None:
    """追加一行审计（best-effort，永不抛）。"""
    path = audit_path(root)
    entry: dict[str, Any] = {"ts": round(time.time(), 3), "event": event, **fields}
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        try:
            if path.stat().st_size > MAX_BYTES:
                os.replace(path, path.with_name(path.name + ".1"))
        except OSError:
            pass
        with path.open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
    except OSError as exc:
        logger.warning(f"[zeta] 审计写入失败（不阻断）：{exc}")


def tail(n: int = 20, *, root: Path | None = None) -> list[dict[str, Any]]:
    """读最近 ``n`` 条（排查用；不作远程接口）。"""
    path = audit_path(root)
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as f:
            lines = f.readlines()[-n:]
        for line in lines:
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
    except OSError:
        return []
    return out
