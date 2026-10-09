"""
memory_index — 记忆检索旁路：SQLite FTS5（可丢弃缓存，损坏即重建）

设计要点（/tmp/fp_memory_tree_design_20261009.md §3.2）：
- trigram 分词（本机 SQLite 3.46 实测）：<3 字符查询静默失效 → 退化为 AND 子串扫描（双轨，裁决 Q3）
- 排序 bm25（= "词云概率最大化"的正确实现；词云得分是启发式，BM25 就是那个 ranked score）
- query 吃同义词组：空格分词 → OR + 排名（LLM 供词扩展的落点）
- 一致性三件套：mtime/name 失配即全量重建（可丢弃）、构建用临时文件 + os.replace（原子）、
  文件消失即从索引移除（tombstone 语义）
- 多实例 LWW 可接受（索引是缓存，真相源是 .md）
"""

from __future__ import annotations

import contextlib
import os
import sqlite3
import tempfile
from typing import Any

from fp_core import config, memory_tree

# FTS5 trigram 对 <3 字符查询静默失效的边界（设计文档实测结论）
_MIN_TERM_LEN = 3


def _db_path() -> str:
    # 动态计算：测试会 monkeypatch config._FP_DATA_DIR
    return os.path.join(config.data_dir(), "cache", "memory_index.sqlite")


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10)
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  name TEXT NOT NULL,
  root TEXT NOT NULL,
  category TEXT NOT NULL,
  path TEXT NOT NULL,
  mtime REAL NOT NULL,
  description TEXT NOT NULL DEFAULT '',
  updated TEXT NOT NULL DEFAULT '',
  PRIMARY KEY(root, name)
);
CREATE VIRTUAL TABLE IF NOT EXISTS docs USING fts5(
  name UNINDEXED, root UNINDEXED, category UNINDEXED, text, tokenize='trigram'
);
"""


def _all_leaves() -> list[dict[str, str]]:
    return memory_tree.merged_leaves("~") + memory_tree.merged_leaves(".")


def _doc_text(leaf: dict[str, str], meta: dict[str, Any]) -> str:
    with open(leaf["path"], encoding="utf-8") as f:
        content = f.read()
    # 正文 = 跳过 frontmatter
    lines = content.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    body_text = "\n".join(lines[body_start:]).strip()
    return "\n".join([
        str(meta.get("name") or leaf["name"]),
        str(meta.get("description") or ""),
        str(meta.get("type") or leaf["category"]),
        str(meta.get("hint") or ""),
        str(meta.get("context") or ""),
        body_text,
    ])


def _needs_rebuild(conn: sqlite3.Connection, leaves: list[dict[str, str]]) -> bool:
    try:
        rows = conn.execute("SELECT root, name, mtime FROM files").fetchall()
    except sqlite3.DatabaseError:
        return True
    current = {(x["root"], x["name"]): float(x["mtime"]) for x in leaves}
    stored = {(r[0], r[1]): float(r[2]) for r in rows}
    if set(current) != set(stored):
        return True
    return any(abs(current[k] - stored[k]) > 0.001 for k in current)


def _rebuild(conn: sqlite3.Connection, leaves: list[dict[str, str]]) -> None:
    """全量重建到临时库再原子替换（崩溃也不留半截索引）"""
    final = _db_path()
    os.makedirs(os.path.dirname(final), exist_ok=True)
    fd, tmp = tempfile.mkstemp(suffix=".sqlite.tmp", dir=os.path.dirname(final))
    os.close(fd)
    try:
        tconn = _connect(tmp)
        tconn.executescript(_SCHEMA)
        for leaf in leaves:
            meta = memory_tree.leaf_meta(leaf)
            tconn.execute(
                "INSERT OR REPLACE INTO files(name, root, category, path, mtime, description, updated)"
                " VALUES(?,?,?,?,?,?,?)",
                (
                    leaf["name"],
                    leaf["root"],
                    leaf["category"],
                    leaf["path"],
                    float(leaf["mtime"]),
                    str(meta.get("description") or ""),
                    str(meta.get("updated") or ""),
                ),
            )
            tconn.execute(
                "INSERT INTO docs(name, root, category, text) VALUES(?,?,?,?)",
                (leaf["name"], leaf["root"], leaf["category"], _doc_text(leaf, meta)),
            )
        tconn.commit()
        tconn.close()
        os.replace(tmp, final)
        # 清掉旧库的 -wal/-shm，避免残留
        for suffix in ("-wal", "-shm"):
            with_open = final + suffix
            if os.path.exists(with_open):
                with contextlib.suppress(OSError):
                    os.unlink(with_open)
    except BaseException:
        if os.path.exists(tmp):
            with contextlib.suppress(OSError):
                os.unlink(tmp)
        raise


def ensure(force: bool = False) -> None:
    """确保索引与磁盘叶子一致（mtime/name 失配 → 全量重建）。失败静默降级（旁路可缺）。"""
    try:
        leaves = _all_leaves()
        path = _db_path()
        if not os.path.isfile(path):
            os.makedirs(os.path.dirname(path), exist_ok=True)
            conn = _connect(path)
            conn.executescript(_SCHEMA)
            conn.commit()
            _rebuild(conn, leaves)
            conn.close()
            return
        conn = _connect(path)
        if force or _needs_rebuild(conn, leaves):
            conn.close()
            conn = _connect(path)
            _rebuild(conn, leaves)
        conn.close()
    except (sqlite3.Error, OSError):
        # 旁路索引失败不阻塞主流程：query 会走 AND 兜底
        pass


def upsert(leaf: dict[str, str], doc_text: str) -> None:
    """单条写入/更新（memory_save 后调用）；失败静默（下次 ensure 会重建）"""
    try:
        ensure()
        meta = memory_tree.leaf_meta(leaf)
        conn = _connect(_db_path())
        conn.execute("DELETE FROM files WHERE root=? AND name=?", (leaf["root"], leaf["name"]))
        conn.execute("DELETE FROM docs WHERE root=? AND name=?", (leaf["root"], leaf["name"]))
        conn.execute(
            "INSERT INTO files(name, root, category, path, mtime, description, updated) VALUES(?,?,?,?,?,?,?)",
            (
                leaf["name"],
                leaf["root"],
                leaf["category"],
                leaf["path"],
                float(leaf["mtime"]),
                str(meta.get("description") or ""),
                str(meta.get("updated") or ""),
            ),
        )
        conn.execute(
            "INSERT INTO docs(name, root, category, text) VALUES(?,?,?,?)",
            (leaf["name"], leaf["root"], leaf["category"], doc_text),
        )
        conn.commit()
        conn.close()
    except (sqlite3.Error, OSError):
        pass


def _fts_query(query: str) -> str:
    """空格分词 → 逐词短语 + OR（同义词组语义，bm25 排名）。

    FTS5 短语必须双引号（单引号是语法错误）；内部 " → "" 转义。
    """
    terms = [t for t in query.split() if t]
    quoted = ['"' + t.replace('"', '""') + '"' for t in terms]
    return " OR ".join(quoted)


def search(query: str, limit: int = 20) -> list[dict[str, str]]:
    """FTS5 排序检索。任一词 <3 字符（trigram 静默失效边界）→ 返回 [] 由调用方走 AND 兜底。"""
    terms = [t for t in query.split() if t]
    if not terms or any(len(t) < _MIN_TERM_LEN for t in terms):
        return []
    try:
        ensure()
        conn = _connect(_db_path())
        rows = conn.execute(
            "SELECT f.name, f.root, f.category, f.description, f.updated"
            " FROM docs JOIN files f ON f.root=docs.root AND f.name=docs.name"
            " WHERE docs MATCH ? ORDER BY bm25(docs) LIMIT ?",
            (_fts_query(query), limit),
        ).fetchall()
        conn.close()
    except (sqlite3.Error, OSError):
        return []
    return [{"name": r[0], "root": r[1], "type": r[2], "description": r[3], "updated": r[4]} for r in rows]
