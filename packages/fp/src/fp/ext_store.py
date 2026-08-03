"""ext store — 注册表与审计日志

ext-registry.json（fetched 资产纳管）：
    {
      "version": 1,
      "assets": {
        "<type>/<name>": {
          "name": "...", "type": "tools",
          "source": "https://github.com/...", "commit": "sha", "sha256": "...",
          "installed_at": "2025-01-01T00:00:00", "status": "active"
        }
      }
    }

ext-audit.jsonl（两段式追加，每行一条）：
    {"ts": "...", "action": "install", "asset": "tools/foo", "origin": "...", "note": "..."}

设计：registry/audit 是元数据，不属于任何 git 仓库，位于 {DATA} 根。
"""

import json
import os
from datetime import datetime

from fp.ext_assets import audit_path, registry_path

REGISTRY_VERSION = 1


# ── registry ────────────────────────────────────────────────────


def load_registry() -> dict:
    """加载注册表，不存在或损坏时返回空结构（幂等）。"""
    path = registry_path()
    if not os.path.isfile(path):
        return {"version": REGISTRY_VERSION, "assets": {}}
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get("assets"), dict):
            return data
    except (json.JSONDecodeError, OSError):
        pass
    return {"version": REGISTRY_VERSION, "assets": {}}


def save_registry(registry: dict) -> None:
    """原子写注册表（先写临时文件再 rename）。"""
    path = registry_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(registry, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def registry_asset(key: str) -> dict | None:
    """按 "<type>/<name>" 查注册表条目。"""
    return load_registry()["assets"].get(key)


def upsert_asset(key: str, data: dict) -> None:
    """新增或更新一条注册表记录。"""
    reg = load_registry()
    reg["assets"][key] = data
    save_registry(reg)


def remove_asset(key: str) -> bool:
    """删除注册表条目，返回是否删除成功。"""
    reg = load_registry()
    if key not in reg["assets"]:
        return False
    del reg["assets"][key]
    save_registry(reg)
    return True


# ── audit ───────────────────────────────────────────────────────


def append_audit(action: str, asset: str, origin: str = "", note: str = "") -> None:
    """追加一条审计记录（jsonl）。"""
    path = audit_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    record = {
        "ts": datetime.now().isoformat(timespec="seconds"),
        "action": action,
        "asset": asset,
        "origin": origin,
        "note": note,
    }
    with open(path, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def load_audit(limit: int | None = None) -> list[dict]:
    """读取审计日志（按时间正序，limit 限制条数）。"""
    path = audit_path()
    if not os.path.isfile(path):
        return []
    records: list[dict] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                records.append(json.loads(line))
            except json.JSONDecodeError:
                continue
    return records[-limit:] if limit else records
