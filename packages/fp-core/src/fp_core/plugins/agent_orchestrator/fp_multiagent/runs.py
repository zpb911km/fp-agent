"""产物存储(runs)— 纯文件读写的库

与任务图(taskmap)的分工:
- **taskmap** = 脊柱(状态 / 契约 / 协作), 单文件 `.fp/tasks.json`, 小。
- **runs**    = 产物(worker 的完整输出 / 日志), 一次性大文本, 按 run_id 分文件。

生命周期: **比会话长**——长任务的证据不该随会话消失; 由任务图节点的 `evidence` 引用。
存储: `{DATA}/runs/<run_id>.json`

(历史: 本模块原名 blackboard, 按会话分目录且会话收尾即擦; 现收窄为「产物存储」,
不再与会话绑定, 因为任务图升为脊柱后, 编排进度归图、产物归此。)
"""

from __future__ import annotations

import contextlib
import json
import os
import tempfile
import time
from typing import Any

from fp_core.platform_utils import get_data_dir

RUNS_ROOT: str = os.path.join(get_data_dir(), "runs")


# ── 路径 ────────────────────────────────────────────


def run_path(run_id: str) -> str:
    return os.path.join(RUNS_ROOT, f"{run_id}.json")


# ── 读写 ────────────────────────────────────────────


def _atomic_write(path: str, data: dict[str, Any]) -> None:
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".run_", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)


def save_run(run_id: str, run: dict[str, Any]) -> str:
    """整份写入某次编排运行的产物归档"""
    path = run_path(run_id)
    run = dict(run)
    run.setdefault("run_id", run_id)
    run["updated_at"] = time.time()
    _atomic_write(path, run)
    return path


def load_run(run_id: str) -> dict[str, Any] | None:
    path = run_path(run_id)
    if not os.path.exists(path):
        return None
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, json.JSONDecodeError):
        return None


def upsert_entry(run_id: str, entry: dict[str, Any]) -> str:
    """按 task_id 插入/更新一条条目(产物归档)"""
    run = load_run(run_id) or {"run_id": run_id, "entries": []}
    entries: list[dict[str, Any]] = run.setdefault("entries", [])
    tid = entry.get("task_id")
    for i, e in enumerate(entries):
        if e.get("task_id") == tid:
            merged = dict(e)
            merged.update(entry)
            entries[i] = merged
            break
    else:
        entries.append(dict(entry))
    return save_run(run_id, run)


def delete_run(run_id: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(run_path(run_id))


def list_runs() -> list[str]:
    if not os.path.isdir(RUNS_ROOT):
        return []
    return sorted(f[:-5] for f in os.listdir(RUNS_ROOT) if f.endswith(".json"))
