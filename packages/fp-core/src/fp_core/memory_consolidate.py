"""记忆巩固检测（P4）— 阈值/孤儿/近重/错抽屉检测 + flock 单写者 + 日门控 + journal

设计定位（/tmp/fp_memory_tree_design_20261009.md §3/§8）：
  - 本模块只做**检测与报告**（机检可判的廉价信号），不做结构搬迁——
    分裂/合并/重归档等语义操作需要 LLM 提案，由后续巩固流程消费本模块的报告。
  - 检测结果三出口：journal（结构化追加日志）、报告 md（给人看的清单）、
    返回摘要（给插件/测试）。
  - 并发：flock 单写者（同 cwd 多实例只有一个在跑，其余立即跳过）。
  - 频控：日门控（同一天内只跑一次，force=True 绕过——测试与手动触发用）。

机检信号清单（全部零 LLM、零正文语义判断）：
  split     节点子项数 > 阈值（默认 20）→ 建议分裂
  merge     节点子项数在 1..MIN（默认 4）→ 建议并入兄弟节点（人/LLM 复核）
  empty     分类目录无叶子
  orphan    created 超过 90 天 且 零访问
  stale     updated 超过 90 天（列表，不自动归档）
  near_dup  同根内 description bigram-Jaccard ≥ 0.6 的成对条目（疑似冲突/重复，交 LLM 复核）
  misfile   分类目录与 frontmatter type 不一致（结构性错抽屉）→ 给出搬迁建议
  forbidden 分类在禁用词表 / 被归入 _legacy
  no_context 缺 context 字段（语义 lint 的机检半边）
"""

from __future__ import annotations

import fcntl
import json
import os
import time
from datetime import date, datetime
from typing import Any, cast

from fp_core import config, memory_tree

# ── 阈值（可配置入口：直接改模块常量；golden set 实测后再调） ──
SPLIT_THRESHOLD = 20  # 节点子项 > 此值 → split 建议
MERGE_MIN = 4  # 节点子项 1..此值 → merge 建议
DUP_JACCARD = 0.6  # description bigram Jaccard ≥ 此值 → near_dup
DAYS_ORPHAN = 90  # created 超过此天数 + 零访问 → orphan
DAYS_STALE = 90  # updated 超过此天数 → stale

# ── 日志路径（{DATA}/journal/） ──
_DATA_DIR = os.path.dirname(config.SESSIONS_DIR)
JOURNAL_DIR = os.path.join(_DATA_DIR, "journal")
LOCK_PATH = os.path.join(JOURNAL_DIR, "memory_consolidate.lock")
GATE_PATH = os.path.join(JOURNAL_DIR, "memory_consolidate.gate")
JOURNAL_PATH = os.path.join(JOURNAL_DIR, "memory_consolidate.jsonl")


def _ensure_journal_dir() -> None:
    os.makedirs(JOURNAL_DIR, exist_ok=True)


def _age_days(date_str: str, now: float) -> float | None:
    """ISO 日期（或带时间前缀）→ 距今天数；不可解析返回 None。"""
    s = (date_str or "").strip()[:10]
    if not s:
        return None
    try:
        dt = datetime.strptime(s, "%Y-%m-%d").timestamp()
    except ValueError:
        return None
    return (now - dt) / 86400.0


def _shingles(text: str) -> set[str]:
    """bigram 片段集（CJK 友好；ASCII 词拆分后同样取 2-gram）。"""
    t = "".join(ch.lower() for ch in text if ch.isalnum() or "一" <= ch <= "鿿")
    if len(t) < 2:
        return {t} if t else set()
    return {t[i : i + 2] for i in range(len(t) - 1)}


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    inter = len(a & b)
    return inter / (len(a) + len(b) - inter)


def detect(root_label: str, now: float | None = None) -> dict[str, Any]:
    """对单根跑全量机检。返回结构化 findings（纯数据，不写任何文件）。"""
    now = time.time() if now is None else now
    groups = memory_tree.categories(root_label)
    access = memory_tree.load_access(root_label)

    findings: dict[str, Any] = {
        "root": root_label,
        "total": 0,
        "split": [],
        "merge": [],
        "empty": [],
        "orphan": [],
        "stale": [],
        "near_dup": [],
        "misfile": [],
        "forbidden": [],
        "no_context": [],
        "no_description": [],
    }

    # 各分类阈值扫描 + 逐叶 frontmatter 检查
    for cat, leaves in sorted(groups.items()):
        findings["total"] += len(leaves)
        if cat.startswith("_") or cat in config.FORBIDDEN_CATEGORIES:
            findings["forbidden"].append({"category": cat, "count": len(leaves)})
        if not leaves:
            findings["empty"].append(cat)
        elif len(leaves) > SPLIT_THRESHOLD:
            findings["split"].append({"category": cat, "count": len(leaves)})
        elif len(leaves) < MERGE_MIN:
            findings["merge"].append({"category": cat, "count": len(leaves)})

        for leaf in leaves:
            meta = memory_tree.leaf_meta(leaf)
            name = leaf["name"]

            # 错抽屉（结构性）：目录分类与 frontmatter type 不一致
            fm_type = str(meta.get("type") or "").strip().lower()
            if fm_type and fm_type != leaf["category"] and not leaf["category"].startswith("_"):
                findings["misfile"].append({
                    "name": name,
                    "category": leaf["category"],
                    "frontmatter_type": fm_type,
                    "suggest": f"{leaf['category']}/{name} → {fm_type}/{name}",
                })

            if not str(meta.get("description") or "").strip():
                findings["no_description"].append(name)
            if not str(meta.get("context") or "").strip():
                findings["no_context"].append(name)

            created = _age_days(str(meta.get("created") or ""), now)
            updated = _age_days(str(meta.get("updated") or ""), now)
            hits = int(access.get(name, 0))
            if created is not None and created >= DAYS_ORPHAN and hits == 0:
                findings["orphan"].append({
                    "name": name,
                    "category": leaf["category"],
                    "created_days_ago": round(created),
                })
            if updated is not None and updated >= DAYS_STALE:
                findings["stale"].append({
                    "name": name,
                    "category": leaf["category"],
                    "updated_days_ago": round(updated),
                })

    # 近重扫描：同根两两比 description bigram Jaccard（291 条 ≈ 4.2 万对，纯内存毫秒级）
    enriched: list[tuple[dict[str, str], set[str]]] = []
    for _cat, leaves in groups.items():
        for leaf in leaves:
            desc = str(memory_tree.leaf_meta(leaf).get("description") or "")
            enriched.append((leaf, _shingles(desc)))
    for i in range(len(enriched)):
        (la, sa) = enriched[i]
        if not sa:
            continue
        for j in range(i + 1, len(enriched)):
            (lb, sb) = enriched[j]
            score = _jaccard(sa, sb)
            if score >= DUP_JACCARD:
                findings["near_dup"].append({
                    "a": f"{la['category']}/{la['name']}",
                    "b": f"{lb['category']}/{lb['name']}",
                    "score": round(score, 2),
                })

    return findings


def _report_path(day: str) -> str:
    return os.path.join(JOURNAL_DIR, f"memory_report_{day}.md")


def _write_report(day: str, all_findings: list[dict[str, Any]]) -> str:
    lines = [
        f"# 记忆巩固检测报告 {day}",
        "",
        "机检信号（零 LLM）。结构搬迁类操作需 LLM 复核本报告后执行。",
        "",
    ]
    section_titles = {
        "split": "建议分裂（子项超阈值）",
        "merge": "建议合并（子项过少）",
        "empty": "空分类",
        "orphan": "孤儿（超 90 天零访问）",
        "stale": "过期（超 90 天未更新）",
        "near_dup": "近重/疑似冲突（交 LLM 复核）",
        "misfile": "错抽屉（目录与 frontmatter type 不符）",
        "forbidden": "禁用/遗留分类",
        "no_context": "缺 context 字段",
        "no_description": "缺 description",
    }
    for f in all_findings:
        lines.append(f"## 根 {f['root']}（共 {f['total']} 条）")
        for key, title in section_titles.items():
            items_raw: Any = f.get(key) or []
            if not isinstance(items_raw, list):
                continue
            items = cast(list[Any], items_raw)
            if not items:
                continue
            lines.append(f"\n### {title}（{len(items)}）")
            if key == "near_dup":
                for it in items:
                    lines.append(f"- {it['a']} ↔ {it['b']}  (jaccard={it['score']})")
            elif key == "misfile":
                for it in items:
                    lines.append(f"- {it['suggest']}")
            elif key in ("orphan", "stale"):
                for it in items:
                    days = it.get("created_days_ago", it.get("updated_days_ago"))
                    lines.append(f"- {it['category']}/{it['name']}（{days} 天）")
            else:
                lines.append("- " + "、".join(str(x) for x in items))
        lines.append("")
    path = _report_path(day)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fp:
        fp.write("\n".join(lines))
    os.replace(tmp, path)
    return path


def _journal(entries: list[dict[str, Any]]) -> None:
    _ensure_journal_dir()
    with open(JOURNAL_PATH, "a", encoding="utf-8") as fp:
        for e in entries:
            fp.write(json.dumps(e, ensure_ascii=False) + "\n")


def run(force: bool = False) -> str:
    """执行一次巩固检测（单写者 + 日门控）。返回人类可读摘要。"""
    _ensure_journal_dir()
    today = date.today().isoformat()

    # ── 日门控 ──
    if not force and os.path.isfile(GATE_PATH):
        try:
            with open(GATE_PATH, encoding="utf-8") as fp:
                if fp.read().strip() == today:
                    return f"巩固检测：日门控生效（{today} 已跑过），跳过（force=True 可绕过）"
        except OSError:
            pass

    # ── flock 单写者（同进程多次 open 亦互斥；拿不到锁 = 别处正在跑，立即让位）──
    lock_fd = os.open(LOCK_PATH, os.O_CREAT | os.O_RDWR, 0o644)
    try:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return "巩固检测：另一实例持锁（单写者），本次跳过"
        try:
            all_findings = [detect("~"), detect(".")]
            entries: list[dict[str, Any]] = []
            for f in all_findings:
                for key in ("split", "merge", "empty", "orphan", "stale", "near_dup", "misfile", "forbidden"):
                    raw_items: Any = f.get(key) or []
                    if not isinstance(raw_items, list):
                        continue
                    for item in cast(list[Any], raw_items):
                        entries.append({
                            "ts": datetime.now().isoformat(timespec="seconds"),
                            "kind": key,
                            "root": f["root"],
                            "item": item,
                        })
            _journal(entries)
            report = _write_report(today, all_findings)

            # 门控盖章
            with open(GATE_PATH, "w", encoding="utf-8") as fp:
                fp.write(today)

            counts = {
                k: sum(len(f.get(k) or []) for f in all_findings)
                for k in ("split", "merge", "near_dup", "misfile", "orphan", "stale")
            }
            return f"巩固检测完成：{counts} | journal={JOURNAL_PATH} | 报告={report}"
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
    finally:
        os.close(lock_fd)
