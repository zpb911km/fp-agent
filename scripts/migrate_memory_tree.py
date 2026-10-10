#!/usr/bin/env python
"""迁移记忆库：扁平布局 → 类别子目录（树布局）。幂等、可回滚、零正文读取。

设计：/tmp/fp_memory_tree_design_20261009.md §3（树布局）与验收 n5。

安全性质：
  - 只读 frontmatter（memory_tree.read_head，前 8KB），绝不读正文全文
  - 先整体备份（tar.gz）再搬迁；逐条记 journal（memory_migration.jsonl）
  - type 缺失/禁用（FORBIDDEN_CATEGORIES）的文件**不移动**（留扁平层，
    由巩固检测报告处理——宁可留在可见处，不塞进更糟的抽屉）
  - 目标目录已存在同名文件 → 冲突，跳过并记录
  - 幂等：已搬迁的根再跑为零操作
  - 搬迁后强制重建派生缓存（_node/_index）与 FTS 索引

用法（仓库根目录）：
  python scripts/migrate_memory_tree.py             # 执行迁移
  python scripts/migrate_memory_tree.py --dry-run   # 只预演，不落盘
  python scripts/migrate_memory_tree.py --rollback  # 按 journal 逆向回搬
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import tarfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "packages", "fp-core", "src"))

from fp_core import config  # noqa: E402
from fp_core.plugins.memory import tree as memory_tree  # noqa: E402


def _journal_path() -> str:
    """调用期计算（测试可 monkeypatch config._FP_DATA_DIR 隔离；
    导入期绑定 SESSIONS_DIR 曾导致隔离测试回滚了真实 journal——事故见
    journal/memory_migration.jsonl.bak_incident1）。"""
    return os.path.join(config._FP_DATA_DIR, "journal", "memory_migration.jsonl")


def _roots() -> list[str]:
    """全局三来源（存在才迁移）+ 当前 cwd 的本地根。"""
    dirs = [d for d in config.user_dirs("memory") if os.path.isdir(d)]
    local = os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)
    if os.path.isdir(local):
        dirs.append(local)
    return dirs


def _backup(root_dirs: list[str]) -> str:
    os.makedirs(os.path.dirname(_journal_path()), exist_ok=True)
    path = os.path.join(
        os.path.dirname(_journal_path()), f"memory_migration_backup_{time.strftime('%Y%m%d_%H%M%S')}.tar.gz"
    )
    with tarfile.open(path, "w:gz") as tar:
        for d in root_dirs:
            tar.add(d, arcname=os.path.basename(d))
    return path


def _journal(entries: list[dict]) -> None:
    if not entries:
        return
    os.makedirs(os.path.dirname(_journal_path()), exist_ok=True)
    with open(_journal_path(), "a", encoding="utf-8") as fp:
        for e in entries:
            fp.write(json.dumps(e, ensure_ascii=False) + "\n")


def migrate(dry_run: bool) -> int:
    root_dirs = _roots()
    if not root_dirs:
        print("未找到任何记忆根目录，无事可做")
        return 0

    backup = None if dry_run else _backup(root_dirs)
    moved = conflicts = skipped = 0
    entries: list[dict] = []

    for mem_dir in root_dirs:
        for entry in sorted(os.listdir(mem_dir)):
            if not entry.endswith(".md") or entry == memory_tree.INDEX_FILE:
                continue
            src = os.path.join(mem_dir, entry)
            if not os.path.isfile(src):
                continue
            fm = memory_tree.parse_frontmatter(memory_tree.read_head(src))
            cat = str(fm.get("type") or "").strip().lower()
            if not cat or cat in config.FORBIDDEN_CATEGORIES:
                skipped += 1
                entries.append({
                    "ts": time.strftime("%Y-%m-%d %H:%M:%S"),
                    "kind": "skip",
                    "src": src,
                    "reason": "invalid_type",
                    "type": cat,
                })
                continue
            dst = os.path.join(mem_dir, cat, entry)
            if os.path.exists(dst):
                conflicts += 1
                entries.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "conflict", "src": src, "dst": dst})
                continue
            if not dry_run:
                os.makedirs(os.path.dirname(dst), exist_ok=True)
                os.rename(src, dst)
            moved += 1
            entries.append({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": "move", "src": src, "dst": dst})

    if not dry_run:
        _journal(entries)
        # 重建派生缓存与 FTS 索引（可丢弃缓存，重建即新鲜）
        for label in ("~", "."):
            memory_tree.ensure_root(label)
        from fp_core.plugins.memory import index as memory_index

        memory_index.ensure(force=True)

    print(
        f"{'[dry-run] ' if dry_run else ''}迁移完成：moved={moved} conflict={conflicts} skipped_invalid_type={skipped}"
    )
    if backup:
        print(f"备份：{backup}")
    print(f"journal：{_journal_path()}")
    return moved


def rollback() -> int:
    if not os.path.isfile(_journal_path()):
        print("无 journal，无法回滚")
        return 0
    moves: list[dict] = []
    with open(_journal_path(), encoding="utf-8") as fp:
        for line in fp:
            if line.strip():
                e = json.loads(line)
                if e.get("kind") == "move":
                    moves.append(e)
    reverted = 0
    for e in reversed(moves):  # 逆序回搬
        src, dst = e["src"], e["dst"]
        if os.path.exists(dst) and not os.path.exists(src):
            os.rename(dst, src)
            reverted += 1
    for label in ("~", "."):
        memory_tree.ensure_root(label)
    print(f"回滚完成：reverted={reverted} / moves={len(moves)}")
    return reverted


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--dry-run", action="store_true", help="只预演，不落盘")
    ap.add_argument("--rollback", action="store_true", help="按 journal 逆向回搬")
    args = ap.parse_args()
    if args.rollback:
        rollback()
    else:
        migrate(args.dry_run)


if __name__ == "__main__":
    main()
