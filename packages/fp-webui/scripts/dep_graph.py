#!/usr/bin/env python3
"""前端依赖图谱生成器：解析 static/ 下 ESM 模块的 import/export*/动态 import 边，
输出 mermaid flowchart（默认）或邻接表（--list）。

用法:
  python3 scripts/dep_graph.py            # mermaid 到 stdout
  python3 scripts/dep_graph.py --list     # 文本邻接表
  python3 scripts/dep_graph.py -o dep.md  # 写文件
  python3 scripts/dep_graph.py --check    # 额外报告: 未解析边 / 循环依赖

零第三方依赖；只读，不改任何文件。
"""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "src" / "fp_webui" / "static"

IMPORT_RE = re.compile(r"""(?:^|\n)\s*import\s+(?:[^'"]*?\s+from\s+)?['"]([^'"]+)[']""")
EXPORT_RE = re.compile(r"""(?:^|\n)\s*export\s+(?:\*|\{[^}]*\})\s*from\s+['"]([^'"]+)[']""")
DYN_RE = re.compile(r"""import\(\s*['"]([^'"]+)['"]""")


def resolve(base: Path, spec: str) -> str | None:
    """把 import 说明符解析为 static/ 内的相对路径；CDN/无法解析返回 None。"""
    if spec.startswith(("http://", "https://", "//")):
        return None
    spec = re.sub(r"^\./+", "", spec)
    while spec.startswith("../"):
        base = base.parent
        spec = spec[3:]
    if spec.startswith("static/"):
        spec = spec[7:]
    cand = (base.parent / spec) if not spec.startswith("/") else STATIC / spec.lstrip("/")
    for c in (cand, cand.with_suffix(".js"), cand / "index.js"):
        try:
            return str(c.resolve().relative_to(STATIC))
        except ValueError:
            continue
    return None  # 解析失败


def collect() -> tuple[dict[str, list[tuple[str, str]]], list[str]]:
    edges: dict[str, list[tuple[str, str]]] = {}
    bad: list[str] = []
    for p in sorted(STATIC.rglob("*.js")):
        if "vendor" in p.parts:
            continue
        rel = str(p.relative_to(STATIC))
        src = p.read_text(encoding="utf-8")
        found: list[tuple[str, str]] = []
        for kind, rx in (("i", IMPORT_RE), ("e", EXPORT_RE), ("d", DYN_RE)):
            for m in rx.finditer(src):
                tgt = resolve(p, m.group(1))
                if tgt is None:
                    if not m.group(1).startswith(("http", "//")):
                        bad.append(f"{rel}: {m.group(1)}")
                    continue
                if (kind, tgt) not in found:
                    found.append((kind, tgt))
        edges[rel] = found
    return edges, bad


def layer_of(f: str) -> str:
    if f.startswith("js/"):
        f = f[3:]  # 所有 JS 整包归入 js/，分层按 js/ 之下判定
    if f in ("main.js", "app.js", "ui.js", "halo.js", "proj.js"):
        return "入口/装配"
    return {"features": "features", "components": "components", "core": "core"}.get(f.split("/")[0], "其它")


def mermaid(edges: dict[str, list[tuple[str, str]]]) -> str:
    groups: dict[str, list[str]] = {}
    for f in edges:
        groups.setdefault(layer_of(f), []).append(f)
    order = ["入口/装配", "features", "components", "core", "其它"]
    out = ["flowchart TD"]
    for g in order:
        if g not in groups:
            continue
        gid = re.sub(r"\W", "_", g)
        out.append(f'  subgraph {gid}["{g}"]')
        for f in sorted(groups[g]):
            nid = re.sub(r"\W", "_", f)
            label = f if "/" not in f else f.split("/")[-1]
            out.append(f'    {nid}["{f}"]'.replace(f'"{f}"', f'"{label}"'))
        out.append("  end")
    for f, deps in edges.items():
        src = re.sub(r"\W", "_", f)
        for kind, t in deps:
            dst = re.sub(r"\W", "_", t)
            arrow = "-.->" if kind == "d" else ("==>" if kind == "e" else "-->")
            out.append(f"  {src} {arrow} {dst}")
    return "\n".join(out)


def find_cycles(edges: dict[str, list[tuple[str, str]]]) -> list[list[str]]:
    seen: set[tuple[str, ...]] = set()
    cycles: list[list[str]] = []

    def dfs(node: str, path: list[str]) -> None:
        if node in path:
            cyc = path[path.index(node) :] + [node]
            key = tuple(sorted(set(cyc)))
            if key not in seen:
                seen.add(key)
                cycles.append(cyc)
            return
        for _, t in edges.get(node, []):
            dfs(t, path + [node])

    for f in edges:
        dfs(f, [])
    return cycles


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="文本邻接表")
    ap.add_argument("--check", action="store_true", help="报告未解析边与循环依赖")
    ap.add_argument("-o", "--out", help="输出文件（默认 stdout）")
    args = ap.parse_args()

    edges, bad = collect()
    if args.list:
        lines = [f"{f} -> {', '.join(t for _, t in d) or '∅'}" for f, d in edges.items()]
        text = "\n".join(lines)
    else:
        text = mermaid(edges)

    if args.check:
        text += f"\n\n<!-- 未解析边: {len(bad)} -->"
        for b in bad:
            text += f"\n<!--   {b} -->"
        cyc = find_cycles(edges)
        text += f"\n<!-- 循环依赖: {len(cyc)} -->"
        for c in cyc:
            text += f"\n<!--   {' → '.join(c)} -->"

    if args.out:
        Path(args.out).write_text(text + "\n", encoding="utf-8")
        print(
            f"written: {args.out}  (nodes={len(edges)}, edges={sum(len(d) for d in edges.values())}, bad={len(bad)})",
            file=sys.stderr,
        )
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
