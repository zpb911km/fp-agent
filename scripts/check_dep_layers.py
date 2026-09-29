#!/usr/bin/env python3
"""check_dep_layers.py — 四层依赖模型全量检查（AST，零第三方依赖）

层级模型（自顶向下，见 docs/self/README.md 第 3 节）：
    L0 基座      stdlib / 第三方库（所有层可用）
    L1 核心内核  fp_core 主流程：core/ config logger platform_utils prompts taskmap
                 + 各注册表包本体（commands/ tools/ plugins/ 的 __init__ 与 base/）
    L2 核心外围  内置扩展单元：单个命令 / tools/extensions/* / plugins/<unit>/*
    L3 主仓库    入口包 fp fp_cli fp_webui fp_acp + scripts/（仓库工具，非 FP 本体）
    L4 用户资产  {DATA}/{fetched,public,private}/ 与项目 .fp/（不在本仓库，仅登记规则）

规则：
    R1 依赖只允许「下层 → 上层」（L2→L1、L3→L1/L2、L4→L1..L3；L1 只准 L0）
    R2 同层只允许「单元内部」互依；L2 跨单元 = ERROR（历史错误：插件互相依赖）
    R3 内核不得静态 import 扩展（L1→L2 = ERROR；加载器的动态回环是设计意图，豁免为 INFO）
    R4 L4 三来源是覆盖序（private>public>fetched），不是互依关系——本脚本不扫 L4

用法:
    python scripts/check_dep_layers.py               # 全量文字报告
    python scripts/check_dep_layers.py --mermaid     # 直出依赖树 mermaid（单元粒度）
    python scripts/check_dep_layers.py -v            # 含 INFO（加载器回环等）

退出码: 0 = 无 ERROR（WARN 不阻断）；1 = 存在 ERROR。
"""

from __future__ import annotations

import argparse
import ast
import sys
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------- 层判定

INTERNAL_ROOTS = ("fp_core", "fp", "fp_cli", "fp_webui", "fp_acp")

# fp_core 下直接归属 L1 内核的子包/模块（库与主流程，非扩展单元）
_KERNEL_SUBPKGS = {"core", "config", "logger", "platform_utils", "prompts", "taskmap", "_version"}
# 注册表基础设施（L1）vs 扩展单元（L2）的分界
_REG_PACKAGE = {
    ("commands", "__init__"),
    ("tools", "__init__"),
    ("tools", "core"),
    ("plugins", "__init__"),
    ("plugins", "base"),
}


def layer_of(mod: str) -> str:
    """模块 → 层。返回 L0/L1/L2/L3。"""
    if mod.startswith("scripts."):
        return "L3"
    root = mod.split(".", 1)[0]
    if root not in INTERNAL_ROOTS:
        return "L0"
    if root != "fp_core":
        return "L3"
    parts = mod.split(".")
    if len(parts) == 1:
        return "L1"
    sub = parts[1]
    if sub in _KERNEL_SUBPKGS:
        return "L1"
    if sub in ("commands", "tools", "plugins"):
        if len(parts) == 2:
            return "L1"  # 注册表包本体（__init__）
        if (sub, parts[2]) in _REG_PACKAGE:
            return "L1"  # tools.core / plugins.base / commands.__init__
        if sub == "commands":
            return "L1" if parts[2] == "__init__" else "L2"
        if sub == "tools":
            return "L1" if parts[2] in ("__init__", "core") else "L2"  # extensions/*
        return "L1" if parts[2] in ("__init__", "base") else "L2"  # plugins/<unit>/*
    return "L1"  # 未知新子包：按内核对待（新增外围须显式归类）


def unit_of(mod: str) -> str:
    """同层互依的判定单元 & mermaid 节点。"""
    if mod.startswith("scripts."):
        return "scripts/"
    parts = mod.split(".")
    root = parts[0]
    if root != "fp_core":
        return root
    if len(parts) >= 3 and parts[1] == "commands" and parts[2] != "__init__":
        return f"cmd:{parts[2]}"
    if len(parts) >= 4 and parts[1] == "tools" and parts[2] == "extensions":
        return f"tool:{parts[3]}"
    if len(parts) >= 3 and parts[1] == "plugins" and parts[2] not in ("__init__", "base"):
        return f"plugin:{parts[2]}"
    if len(parts) >= 2:
        return "fp_core." + parts[1] if parts[1] in _KERNEL_SUBPKGS else "fp_core"
    return "fp_core"


ALLOWED = {
    "L1": {"L0"},
    "L2": {"L0", "L1"},
    "L3": {"L0", "L1", "L2"},
    "L4": {"L0", "L1", "L2", "L3"},
}

# ---------------------------------------------------------------- 扫描


def resolve_module(file_mod: str, node: ast.ImportFrom) -> str | None:
    if node.level:  # 相对导入
        base = file_mod.split(".")[: -node.level]
        if node.module:
            return ".".join(base + node.module.split("."))
        return ".".join(base)
    return node.module


def scan(repo: Path):
    """→ (edges, dynamic_edges)  edges: {(src, tgt, line)}"""
    edges: set[tuple[str, str, int]] = set()
    dyn_unresolved: set[tuple[str, str, int]] = set()  # 动态 import（非常量参数）
    modules: set[str] = set()

    files: list[tuple[Path, str]] = []  # (path, modname)
    for src in sorted(repo.glob("packages/*/src")):
        for p in src.rglob("*.py"):
            if "egg-info" in str(p) or "__pycache__" in str(p) or "/tests/" in str(p).replace("\\", "/"):
                continue
            rel = p.relative_to(src).with_suffix("")
            files.append((p, ".".join(rel.parts)))
    for p in sorted((repo / "scripts").glob("*.py")):
        files.append((p, f"scripts.{p.stem}"))

    for path, mod in files:
        modules.add(mod)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except SyntaxError as e:
            print(f"[WARN] 语法错误，跳过 {path}: {e}", file=sys.stderr)
            continue
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                if isinstance(node, ast.Import):
                    for a in node.names:
                        edges.add((mod, a.name, node.lineno))
                else:
                    tgt = resolve_module(mod, node)
                    if tgt:
                        edges.add((mod, tgt, node.lineno))
            elif isinstance(node, ast.Call):
                fn = node.func
                name = fn.attr if isinstance(fn, ast.Attribute) else (fn.id if isinstance(fn, ast.Name) else None)
                if name in ("import_module", "__import__", "spec_from_file_location"):
                    if name == "spec_from_file_location":
                        arg = node.args[0].value if node.args and isinstance(node.args[0], ast.Constant) else "<path>"
                        dyn_unresolved.add((mod, f"<file:{arg}>", node.lineno))
                    elif node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
                        arg = node.args[0].value
                        if arg.startswith("."):  # 相对动态导入：按当前模块包解析
                            level = len(arg) - len(arg.lstrip("."))
                            rest = arg.lstrip(".")
                            base = mod.split(".")[:-level]
                            tgt = ".".join([p for p in base + rest.split(".") if p])
                        else:
                            tgt = arg
                        edges.add((mod, tgt, node.lineno))
                    else:
                        dyn_unresolved.add((mod, "<dynamic>", node.lineno))
    return edges, dyn_unresolved, modules


# ---------------------------------------------------------------- 检查


def check(edges, dyn):
    errors, warns, infos = [], [], []
    for src, tgt, line in sorted(edges):
        ls, lt = layer_of(src), layer_of(tgt)
        if tgt.split(".", 1)[0] not in INTERNAL_ROOTS and not tgt.startswith("scripts."):
            lt = "L0"
        us, ut = unit_of(src), unit_of(tgt)

        if ls == lt:
            if ls == "L1":
                continue  # 内核内部互依：允许
            if us == ut:
                continue  # 同单元内部：允许
            if ls == "L2":
                errors.append(f"L2 跨单元互依  {src} → {tgt}  (line {line})  [{us} ↛ {ut}]")
            else:
                warns.append(f"L3 同层跨包    {src} → {tgt}  (line {line})  [{us} ↛ {ut}]")
            continue
        if lt in ALLOWED[ls]:
            continue  # 下层 → 上层：合法
        errors.append(f"反向依赖({ls}→{lt})  {src} → {tgt}  (line {line})")

    # 动态加载：注册表/加载器发起 = 运行时回环（设计意图）；其他模块发起 = 需人工确认
    loaders = ("fp_core.tools", "fp_core.commands", "fp_core.plugins.base")
    for src, tgt, line in sorted(dyn):
        if any(src == ld or src.startswith(ld + ".") for ld in loaders):
            infos.append(f"加载器运行时回环（豁免）  {src} → {tgt}  (line {line})")
        else:
            warns.append(f"非常量动态 import（需人工确认）  {src} → {tgt}  (line {line})")
    return errors, warns, infos


# ---------------------------------------------------------------- mermaid


def mermaid(edges, errors) -> str:
    label = {"L0": "L0 运行基座", "L1": "L1 核心内核", "L2": "L2 核心外围资产", "L3": "L3 主仓库"}

    def layer_of_tgt(tgt: str) -> str:
        if tgt.split(".", 1)[0] not in INTERNAL_ROOTS and not tgt.startswith("scripts."):
            return "L0"
        return layer_of(tgt)

    # 违规对：在单元粒度上重放同一套层规则（避免从错误串反解析）
    err_pairs: set[tuple[str, str]] = set()
    for src, tgt, _ in edges:
        ls, lt = layer_of(src), layer_of_tgt(tgt)
        us, ut = unit_of(src), unit_of(tgt)
        if ls == lt and ls in ("L2", "L3") and us != ut or lt not in ALLOWED.get(ls, {"L0"}) and ls != lt:
            err_pairs.add((us, ut))

    agg: dict[tuple[str, str], int] = defaultdict(int)
    nodes: dict[str, str] = {}
    for src, tgt, _ in edges:
        lt = layer_of_tgt(tgt)
        tgt_u = "L0_基座" if lt == "L0" else unit_of(tgt)
        nodes[tgt_u] = lt
        su = unit_of(src)
        nodes[su] = layer_of(src)
        if su == tgt_u:
            continue
        agg[(su, tgt_u)] += 1

    def nid(u: str) -> str:
        return u.replace(":", "_").replace("/", "_").replace(".", "_").replace("-", "_")

    lines = ["graph TD"]
    for layer in ("L0", "L1", "L2", "L3"):
        members = sorted(u for u, lyr in nodes.items() if lyr == layer)
        if not members:
            continue
        lines.append(f'  subgraph {layer}["{label[layer]}"]')
        for u in members:
            lines.append(f'    {nid(u)}["{u}"]')
        lines.append("  end")

    for (s, t), n in sorted(agg.items()):
        if (s, t) in err_pairs or (t, s) in err_pairs:
            lines.append(f'  {nid(s)} -.->|"⛔ {n}"| {nid(t)}')
        else:
            lines.append(f"  {nid(s)} -->|{n}| {nid(t)}")
    lines.append("")
    lines.append("  %% 实线 = 合法依赖（下层→上层/同单元）；虚线⛔ = 违规边")
    lines.append("  %% L0 节点聚合了 stdlib/第三方目标")
    return "\n".join(lines)


# ---------------------------------------------------------------- main


def main() -> int:
    ap = argparse.ArgumentParser(description="四层依赖模型全量检查")
    ap.add_argument("--repo", default=None, help="仓库根（默认：脚本所在目录的上级）")
    ap.add_argument("--mermaid", action="store_true", help="输出 mermaid 依赖树")
    ap.add_argument("-v", "--verbose", action="store_true", help="含 INFO")
    args = ap.parse_args()

    repo = Path(args.repo) if args.repo else Path(__file__).resolve().parent.parent
    edges, dyn, modules = scan(repo)
    errors, warns, infos = check(edges, dyn)

    if args.mermaid:
        print(mermaid(edges, errors))
        return 1 if errors else 0

    print(
        f"扫描 {len(modules)} 个模块，内部依赖边 {sum(1 for s, t, _ in edges if layer_of(t) != 'L0')} 条"
        f"（对外部 {sum(1 for s, t, _ in edges if layer_of(t) == 'L0')} 条）"
    )
    for tag, items in (
        ("ERROR", errors),
        ("WARN ", warns),
        ("INFO ", infos if args.verbose else []),
    ):
        for it in items:
            print(f"[{tag}] {it}")
    print(f"结论: {len(errors)} error / {len(warns)} warn / {len(infos)} info")
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
