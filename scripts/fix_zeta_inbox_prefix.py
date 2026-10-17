#!/usr/bin/env python3
"""一次性修复：把历史上「带 ``peer:`` 前缀投递」造成的错位收件箱归位。

背景：邻居简报/铃声里写的是展示名 ``peer:<name>``，LLM 照抄进 ``peer_send`` 的 ``to``，
旧版传输层未归一化 → 消息落到 ``<root>/peer<name>.inbox``，而收件人轮询的是
``<root>/<name>.inbox``：落盘成功、回执报「已留话」，却永远没人读（静默丢件）。

本脚本以**信封里的 ``to``** 为准（它记录的正是发送方的本意）重新寻址：

- 已过 TTL 的留话 → 删除（``poll`` 反正会丢弃，留着重占空间且让 GC 不敢回收目录）；
- 未过期 → 原子移动到正确的 ``<name>.inbox``（重名则跳过，保先到的）；
- 目录清空后回收空壳（含空的 ``.processed``）。

默认 dry-run，``--apply`` 才动盘。
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import time
from pathlib import Path
from typing import Any, cast

from fp_core.plugins.zeta.discovery import default_peers_dir
from fp_core.plugins.zeta.domain import normalize_peer_name
from fp_core.plugins.zeta.transport import DirTransport


def _load(path: Path) -> dict[str, Any] | None:
    """读一封消息文件；坏文件返回 None（不阻断整批修复）。"""
    try:
        raw: Any = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return cast(dict[str, Any], raw) if isinstance(raw, dict) else None


def _env_to(path: Path) -> str | None:
    """信封里的收件人（发送方的**本意**，也是重新寻址的唯一依据）。"""
    env = _load(path)
    to: Any = env.get("to") if env is not None else None
    return str(to) if isinstance(to, str) and to.strip() else None


def _expired(path: Path, now: float) -> bool:
    env = _load(path)
    ttl: Any = env.get("ttl") if env is not None else None
    ttl_d = cast(dict[str, Any], ttl) if isinstance(ttl, dict) else {}
    deadline: Any = ttl_d.get("deadline")
    try:
        return float(deadline) <= now
    except (TypeError, ValueError):
        return False


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--root", default="", help="发现目录（默认 default_peers_dir()）")
    ap.add_argument("--apply", action="store_true", help="真正动盘（默认只报告）")
    args = ap.parse_args(argv)

    root = Path(args.root) if args.root else default_peers_dir()
    transport = DirTransport(root)
    now = time.time()
    moved = dropped = deduped = 0
    strays: list[str] = []

    for d in sorted(root.glob("*.inbox")):
        if not d.is_dir() or not d.name.startswith("peer"):
            continue
        strays.append(d.name)
        remaining = 0
        for f in sorted(d.glob("msg-*.json")):
            to = _env_to(f)
            name = normalize_peer_name(to or "")
            if not name:
                print(f"  ? 无法判定收件人，保留：{f.name}")
                remaining += 1
                continue
            target_dir = transport.inbox_dir(name)
            if _expired(f, now):
                dropped += 1
                print(f"  - 过期删除 {d.name}/{f.name}（收件人 {name}）")
                if args.apply:
                    f.unlink(missing_ok=True)
                continue
            dst = target_dir / f.name
            if dst.exists() or (target_dir / ".processed" / f.name).exists():
                deduped += 1
                print(f"  = 目标已有同 id，跳过 {d.name}/{f.name} → {target_dir.name}")
                if args.apply:
                    f.unlink(missing_ok=True)
                continue
            moved += 1
            print(f"  → {d.name}/{f.name} → {target_dir.name}（收件人 {name}）")
            if args.apply:
                target_dir.mkdir(parents=True, exist_ok=True)
                os.replace(f, dst)
        if args.apply:
            done = d / ".processed"
            if done.is_dir() and not any(done.iterdir()):
                done.rmdir()
            if not any(d.iterdir()):
                shutil.rmtree(d, ignore_errors=True)

    mode = "已执行" if args.apply else "dry-run（加 --apply 才动盘）"
    print(f"\n{mode}：错位收件箱 {len(strays)} 个 · 归位 {moved} 条 · 过期删除 {dropped} 条 · 去重跳过 {deduped} 条")
    for s in strays:
        print(f"  · {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
