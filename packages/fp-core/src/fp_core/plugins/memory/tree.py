"""
memory_tree — 记忆树布局与派生缓存生成器（设计：/tmp/fp_memory_tree_design_20261009.md）

布局： <mem_dir>/<category>/<name>.md + <mem_dir>/<category>/_node.md + <mem_dir>/_index.md

原则：
- 叶子 .md 是唯一真相源；_index.md / _node.md 是**派生缓存**（可丢弃、可整体重建）
- 合并语义 = 现有扁平布局的直接扩展：全局三来源按 (category, name) 相对路径去重，private 胜
  （后加载覆盖先加载：fetched → public → private）
- 手动编辑优先：派生文件仅在其比子项**旧**（stale）时才重生成——用户手改后 mtime 最新，
  机器不会覆盖（除非随后又有新叶子写入）
- 会话内冻结：_index.md 由 PromptBuilder 在会话构建时确保新鲜并读入，会话中途不再改写
- 兼容扁平遗留：ext 分发/未迁移的根目录 *.md 视为 type 对应类别的叶子（legacy flat）
"""

from __future__ import annotations

import os
from datetime import datetime
from typing import Any, cast

from fp_core import config

# 派生文件名
INDEX_FILE = "_index.md"
NODE_FILE = "_node.md"

# ── 静态词表用途（level-0 的"一行用途"；词表由 lint 周期性从实际目录再生成）──
# 裁决 Q2：允许创建新分类（B 树分裂需要），词表外仅 lint 报告不拦截。
VOCAB_PURPOSE: dict[str, str] = {
    "skill": "技能与操作方法",
    "reference": "环境/工具参考事实",
    "project": "项目状态与决策",
    "user": "用户偏好与身份",
    "feedback": "纠错与行为要求",
    "communication": "对外联系入口",
    "architecture": "架构与机制设计",
    "fix": "故障修复记录",
    "refactor": "重构经验",
    "charter": "章程与准则",
}


def purpose_of(category: str) -> str:
    return VOCAB_PURPOSE.get(category, "分类")


def local_root_dir() -> str:
    """本地树根（项目内 .fp/memory）"""
    return os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)


def global_root_dirs() -> list[str]:
    """全局树三来源目录（低→高，private 最后=胜出方）"""
    return config.user_dirs("memory")


def primary_dir(root_label: str) -> str:
    """派生文件写入的主目录：全局=private（最高优先级），本地=项目目录"""
    return global_root_dirs()[-1] if root_label == "~" else local_root_dir()


# ── 前沿元数据解析（只读文件头部，不加载正文——迁移/扫描保护 token 与 IO）──


def parse_frontmatter(content: str) -> dict[str, Any]:
    """解析 YAML frontmatter（第一对 --- 之间），yaml 失败降级行扫描。"""
    import yaml

    lines = content.split("\n")
    if not lines or lines[0].strip() != "---":
        return {}
    end = 1
    while end < len(lines) and lines[end].strip() != "---":
        end += 1
    if end >= len(lines):
        return {}
    fm_text = "\n".join(lines[1:end])
    try:
        fm = yaml.safe_load(fm_text)
        if isinstance(fm, dict):
            return cast(dict[str, Any], fm)
    except Exception:
        pass
    result: dict[str, Any] = {}
    for line in fm_text.split("\n"):
        for key in ("name", "type", "description", "created", "updated", "hint", "context"):
            if line.startswith(f"{key}:"):
                result[key] = line.split(":", 1)[1].strip()
    return result


def read_head(path: str, limit: int = 8192) -> str:
    """读取文件头部（frontmatter 足够），失败返回空串"""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read(limit)
    except OSError:
        return ""


# ── 扫描：合并视图（类别目录 + 遗留扁平文件）──


def leaves_in_dir(mem_dir: str) -> list[dict[str, str]]:
    """单目录叶子扫描：category/ 子目录内的 *.md + 根下遗留扁平 *.md（按 fm.type 归类）。"""
    out: list[dict[str, str]] = []
    if not os.path.isdir(mem_dir):
        return out
    for entry in sorted(os.listdir(mem_dir)):
        fpath = os.path.join(mem_dir, entry)
        if os.path.isdir(fpath):
            if entry.startswith("_"):
                continue
            for fname in sorted(os.listdir(fpath)):
                if fname.endswith(".md") and fname != NODE_FILE:
                    p = os.path.join(fpath, fname)
                    out.append({
                        "name": os.path.splitext(fname)[0],
                        "category": entry,
                        "path": p,
                        "mtime": str(os.path.getmtime(p)),
                    })
        elif entry.endswith(".md") and entry != INDEX_FILE:
            fm = parse_frontmatter(read_head(fpath))
            cat = str(fm.get("type") or "").strip().lower()
            if not cat or cat in config.FORBIDDEN_CATEGORIES:
                cat = "_legacy"  # 词表外/无分类的遗留文件归入 _legacy（lint 报告）
            out.append({
                "name": os.path.splitext(entry)[0],
                "category": cat,
                "path": fpath,
                "mtime": str(os.path.getmtime(fpath)),
            })
    return out


def merged_leaves(root_label: str) -> list[dict[str, str]]:
    """根的合并视图：全局=三来源按 (category, name) 去重 private 胜；本地=单来源。"""
    dirs = global_root_dirs() if root_label == "~" else [local_root_dir()]
    merged: dict[tuple[str, str], dict[str, str]] = {}
    for d in dirs:  # 低→高，后覆盖前
        for leaf in leaves_in_dir(d):
            leaf["root"] = root_label
            leaf["primary_dir"] = dirs[-1]
            merged[(leaf["category"], leaf["name"])] = leaf
    return list(merged.values())


def categories(root_label: str) -> dict[str, list[dict[str, str]]]:
    groups: dict[str, list[dict[str, str]]] = {}
    for leaf in merged_leaves(root_label):
        groups.setdefault(leaf["category"], []).append(leaf)
    return groups


def leaf_meta(leaf: dict[str, str]) -> dict[str, Any]:
    """frontmatter 元数据（description/hint/updated/accesses…）"""
    return parse_frontmatter(read_head(leaf["path"]))


# ── 派生文件路径 ──


def node_path(root_label: str, category: str) -> str:
    return os.path.join(primary_dir(root_label), category, NODE_FILE)


def index_path(root_label: str) -> str:
    return os.path.join(primary_dir(root_label), INDEX_FILE)


def access_db_path(root_label: str) -> str:
    """访问计数侧文件（派生缓存；叶子文件永不因读取被改写——手动优先）"""
    return os.path.join(primary_dir(root_label), "_access.json")


def load_access(root_label: str) -> dict[str, int]:
    import json

    try:
        with open(access_db_path(root_label), encoding="utf-8") as f:
            data = json.load(f)
        return cast(dict[str, int], data) if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def bump_access(root_label: str, name: str) -> None:
    """精确读取一次 → 计数 +1（热键提升的数据源）"""
    import json

    path = access_db_path(root_label)
    counts = load_access(root_label)
    counts[name] = int(counts.get(name, 0)) + 1
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.{os.getpid()}.tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(counts, f, ensure_ascii=False)
        os.replace(tmp, path)
    except OSError:
        pass


# ── stale 判定（手动编辑优先的核心机制）──


def _max_child_mtime(root_label: str, category: str | None = None) -> float:
    leaves = merged_leaves(root_label)
    if category is not None:
        leaves = [x for x in leaves if x["category"] == category]
    mtimes = [float(x["mtime"]) for x in leaves]
    # 派生依赖还包含同级派生文件：索引依赖节点文件
    if category is None:
        for d in global_root_dirs() if root_label == "~" else [local_root_dir()]:
            for cat in categories(root_label):
                np = os.path.join(d, cat, NODE_FILE)
                if os.path.isfile(np):
                    mtimes.append(os.path.getmtime(np))
    return max(mtimes, default=0.0)


def is_node_stale(root_label: str, category: str) -> bool:
    np = node_path(root_label, category)
    if not os.path.isfile(np):
        return True
    return os.path.getmtime(np) < _max_child_mtime(root_label, category)


def is_index_stale(root_label: str) -> bool:
    ip = index_path(root_label)
    if not os.path.isfile(ip):
        return True
    return os.path.getmtime(ip) < _max_child_mtime(root_label)


# ── 派生文件生成 ──


def _hotkeys(root_label: str, group: list[dict[str, str]], limit: int = 2) -> list[str]:
    """类别热键：有 hint 的叶子按访问计数降序取 top-N（B+ 树 separator key 移植）。"""
    counts = load_access(root_label)
    candidates: list[tuple[int, str]] = []
    for leaf in group:
        meta = leaf_meta(leaf)
        hint = str(meta.get("hint") or "").strip()
        if hint:
            h = hint if len(hint) <= 40 else hint[:40] + "…"
            candidates.append((counts.get(leaf["name"], 0), f"{leaf['name']}→{h}"))
    candidates.sort(key=lambda x: -x[0])
    return [label for _, label in candidates[:limit]]


def gen_node_lines(root_label: str, category: str) -> str:
    """level-1：节点文件内容——子条目一行摘要 + 逃生舱提示。"""
    group = [x for x in merged_leaves(root_label) if x["category"] == category]
    lines = [f"📂 {root_label}/{category}/（共 {len(group)} 条）", ""]
    for leaf in sorted(group, key=lambda x: x["name"]):
        meta = leaf_meta(leaf)
        desc = str(meta.get("description") or "").strip()
        hint = str(meta.get("hint") or "").strip()
        label = f"{leaf['name']}→{hint[:40]}" if hint else leaf["name"]
        updated = str(meta.get("updated") or "")
        suffix = f"（🕒 {updated}）" if updated else ""
        lines.append(f"{label:40s} — {desc}{suffix}")
    lines.append("")
    lines.append("└─ 未找到？升级：memory_read(query=...) 跨类搜索 → memory_read(all=true) 全量扫描")
    return "\n".join(lines) + "\n"


def gen_index_lines(root_label: str) -> str:
    """level-0：根索引文件内容——类别完整 + 热键 + 写死的兜底规则（O(1) 注入预算）。"""
    groups = categories(root_label)
    total = sum(len(g) for g in groups.values())
    if total == 0:
        return ""
    if root_label == "~":
        head = f"## 我的长期记忆索引（~/ 全局，共 {total} 条）"
    else:
        head = f"## 我的本地记忆索引（📍 ./，共 {total} 条）"
    lines = [head]
    for cat in sorted(groups):
        purpose = purpose_of(cat)
        lines.append(f"  {cat}({len(groups[cat])}): {purpose}")
    hot: list[str] = []
    for cat in sorted(groups):
        hot += _hotkeys(root_label, groups[cat])
    if hot:
        lines.append("  热键: " + " | ".join(hot[:5]))
    lines.append("")
    lines.append("━ 用法 ━ memory_read(name=精确读取) / (path='~/<类>'下钻) / (query=搜索) / (all=true全量)")
    lines.append("⚠️ 类别外也可能存在记忆：query miss 后必须 all=true 全量扫描，才准报告「没有」")
    return "\n".join(lines) + "\n"


def _atomic_write(path: str, content: str) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = f"{path}.{os.getpid()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        import contextlib

        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def refresh_node(root_label: str, category: str, force: bool = False) -> bool:
    """节点派生缓存按需重建；不 stale 则跳过（用户手改=最新，不覆盖）。返回是否重写。"""
    if category.startswith("_"):
        return False
    if not force and not is_node_stale(root_label, category):
        return False
    _atomic_write(node_path(root_label, category), gen_node_lines(root_label, category))
    return True


def refresh_index(root_label: str, force: bool = False) -> bool:
    """根索引派生缓存按需重建。返回是否重写。"""
    content = gen_index_lines(root_label)
    if not content:
        return False
    if not force and not is_index_stale(root_label):
        return False
    _atomic_write(index_path(root_label), content)
    return True


def ensure_root(root_label: str) -> str:
    """确保派生缓存新鲜，返回根索引路径（空树返回空串）"""
    if not merged_leaves(root_label):
        return ""
    for cat in categories(root_label):
        refresh_node(root_label, cat)
    refresh_index(root_label)
    return index_path(root_label)


def read_index(root_label: str) -> str:
    """读取 level-0（确保新鲜后读盘——用户手改的 _index.md 优先于机器版本）"""
    path = ensure_root(root_label)
    if not path:
        return ""
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def read_node(root_label: str, category: str) -> str:
    """读取 level-1（确保新鲜后读盘）；类别无叶子 → 空串（交给调用方报「该分类下无记忆」）"""
    if not any(x["category"] == category for x in merged_leaves(root_label)):
        return ""
    refresh_node(root_label, category)
    try:
        with open(node_path(root_label, category), encoding="utf-8") as f:
            return f.read()
    except OSError:
        return ""


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M")
