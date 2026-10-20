"""
会话内容检索

按**正文内容**搜索历史会话。与 ``session_keywords`` 的分工：

- 关键词（session_keywords）：解决「这条会话是干什么的」——识别，列表里一眼看
- 本文检索（本模块）：解决「我记得聊过 X，在哪个会话」——检索，主动查找

关键词只存 top-3，用户想起的词大概率不在里面，所以检索必须落到正文。

实现分两步（先粗筛再精判）：

1. **候选粗筛**：ripgrep 全文匹配，逐词缩小（多词 = AND 语义）。
   实测 906 个会话 / 309MB 全库一次匹配 39ms，因此**不建倒排索引**——
   索引的维护成本远高于实时扫描，而实时扫描足够快。
2. **精确判定**：解析 JSONL 命中行，只认 ``user`` / ``assistant`` 的
   ``content``。tool 返回值占语料 50%，若不过滤，搜任何词都会命中一堆
   日志文件内容，把真正的对话淹掉。

``rg`` 不可用时自动退化为纯 Python 扫描（慢约 10 倍，功能等价）。
"""

import json
import shutil
import subprocess
from pathlib import Path
from typing import Any, cast

from fp_core import config

# 命中片段：命中词前后各取多少字符
SNIPPET_RADIUS = 40
# 每次搜索最多返回多少个会话
DEFAULT_LIMIT = 30
# 单个会话最多展示几条命中片段
MAX_SNIPPETS = 3
# 参与检索的角色（tool 返回 / reasoning 一律不进检索范围）
SEARCHABLE_ROLES = ("user", "assistant")


def sessions_dir() -> Path:
    """会话文件目录（延迟取值，便于测试注入）"""
    return Path(config.SESSIONS_DIR)


def _session_files(directory: Path) -> list[Path]:
    if not directory.is_dir():
        return []
    return sorted(directory.glob("*.jsonl"))


def _rg_narrow(files: list[Path], term: str) -> list[Path]:
    """用 ripgrep 把候选文件缩小到「含 term」的集合。

    rg 缺失或执行失败时返回原集合（交给精确判定兜底），绝不因此漏结果。
    """
    rg = shutil.which("rg")
    if not rg or not files:
        return files
    try:
        proc = subprocess.run(  # noqa: S603 - 参数列表形式，无 shell 注入面
            [rg, "-l", "-i", "-F", "--color", "never", "--", term, *map(str, files)],
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return files
    if proc.returncode not in (0, 1):  # 1 = 无匹配，属正常
        return files
    hit = {Path(line) for line in proc.stdout.splitlines() if line.strip()}
    # 保留目录内文件，且顺序稳定
    return [f for f in files if f in hit]


def _python_narrow(files: list[Path], term: str) -> list[Path]:
    """纯 Python 粗筛回退路径（rg 不可用时）"""
    needle = term.casefold().encode()
    kept: list[Path] = []
    for path in files:
        try:
            if needle in path.read_bytes().lower():
                kept.append(path)
        except OSError:
            continue
    return kept


def _read_meta(path: Path) -> dict[str, Any]:
    """读第 1 行 meta（格式约定见 session.py 模块文档）"""
    try:
        with path.open(encoding="utf-8") as fh:
            loaded = json.loads(fh.readline())
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(loaded, dict):
        return {}
    meta = cast("dict[str, Any]", loaded)
    return meta if meta.get("__meta__") else {}


def _iter_searchable(path: Path):
    """产出 (role, content)——只含参与检索的角色及其非空正文"""
    try:
        with path.open(encoding="utf-8") as fh:
            next(fh, None)  # 跳过 meta 行
            for line in fh:
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, dict):
                    continue
                record = cast("dict[str, Any]", msg)
                role = record.get("role")
                content = record.get("content")
                if role in SEARCHABLE_ROLES and isinstance(content, str) and content.strip():
                    yield role, content
    except OSError:
        return


def _snippet(content: str, term: str) -> str:
    """取命中词前后的上下文，压成单行"""
    flat = " ".join(content.split())
    idx = flat.casefold().find(term.casefold())
    if idx < 0:
        return flat[: SNIPPET_RADIUS * 2]
    start = max(0, idx - SNIPPET_RADIUS)
    end = min(len(flat), idx + len(term) + SNIPPET_RADIUS)
    text = flat[start:end]
    return ("…" if start > 0 else "") + text + ("…" if end < len(flat) else "")


def search_sessions(
    terms: list[str],
    *,
    directory: Path | None = None,
    limit: int = DEFAULT_LIMIT,
) -> list[dict[str, Any]]:
    """按正文检索会话，返回按「更新时间降序」排列的结果。

    参数 ``terms`` 多词之间是 AND：会话必须命中全部词才算匹配。
    返回每项::

        {"sid": str, "meta": dict, "snippets": [{"role","text"}], "hits": int}

    正文无命中但元数据（标题/关键词）命中的会话**不在本函数范围**——
    元数据匹配由调用方（resume 命令）用现成字段完成，二者合并展示。
    """
    words = [w for w in (t.strip() for t in terms) if w]
    if not words:
        return []

    base = directory or sessions_dir()
    candidates = _session_files(base)
    for word in words:
        if not candidates:
            break
        narrowed = _rg_narrow(candidates, word)
        if narrowed == candidates and not shutil.which("rg"):
            narrowed = _python_narrow(candidates, word)
        candidates = narrowed

    results: list[dict[str, Any]] = []
    for path in candidates:
        snippets: list[dict[str, str]] = []
        hits = 0
        for role, content in _iter_searchable(path):
            folded = content.casefold()
            matched = [w for w in words if w.casefold() in folded]
            if not matched:
                continue
            hits += len(matched)
            if len(snippets) < MAX_SNIPPETS:
                snippets.append({"role": role, "text": _snippet(content, matched[0])})
        if not snippets:
            # 候选仅因 tool 返回命中而入选——排除，避免日志噪声冒充对话
            continue
        meta = _read_meta(path)
        results.append({
            "sid": str(meta.get("id") or path.stem),
            "meta": meta,
            "snippets": snippets,
            "hits": hits,
        })

    results.sort(key=lambda r: str(r["meta"].get("updated") or ""), reverse=True)
    return results[:limit]
