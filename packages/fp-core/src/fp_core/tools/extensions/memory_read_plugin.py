"""
Memory Read 插件 v3 — 树导航读取（设计：/tmp/fp_memory_tree_design_20261009.md）

五种入口（逃生梯的机械载体）：
  ① memory_read()                    → level-0 根索引（类别完整 + 热键，会话内冻结的注入源）
  ② memory_read(name=...)            → 精确读取（本地优先）+ 访问计数（热键数据源）
  ③ memory_read(path="~/skill")      → level-1 下钻（_node.md 子条目一行摘要 + 逃生舱提示）
  ④ memory_read(query=...)           → FTS5 bm25 排序检索（同义词组 OR）；短词/miss → AND 子串兜底
  ⑤ memory_read(all=true)            → 逃生梯第 3 档：全量一行摘要扫描（≈旧全量索引，一次工具调用）

铁律：query miss 后必须 all=true 才准报告「没有」（索引尾部写死此规则）。
"""

import asyncio
import os
import re
from datetime import datetime
from typing import Any

from fp_core import config, memory_index, memory_tree

# 记忆新鲜度阈值：updated 距今超过该天数时，精确读取会附加过期提醒
STALE_DAYS = 30

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_read",
        "description": (
            "读取已保存的长期记忆（跨会话）。用法：空参=根索引；name=精确读取；"
            "path=按类别下钻；query=排序搜索；all=true=全量扫描。"
            "铁律：query 无结果后必须 all=true 扫描过，才准报告「没有这条记忆」。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "精确读取一条记忆（从当前可见上下文中取）。同名冲突时本地优先。",
                },
                "path": {
                    "type": "string",
                    "description": "下钻浏览类别，如'~/skill'或'./project'。以 ~/ 或 ./ 开头表示根。",
                },
                "query": {
                    "type": "string",
                    "description": "排序搜索（空格=同义词组 OR，bm25 排名）；短查询自动退化子串匹配。",
                },
                "all": {
                    "type": "boolean",
                    "description": "true=全量一行摘要扫描（逃生舱最后一档：query miss 后的强制穷尽检查）。",
                },
            },
            "description": "五种用法：①空参→根索引 ②name→精确 ③path→下钻 ④query→搜索 ⑤all=true→全量扫描",
        },
    },
}


def _parse_frontmatter(content: str) -> dict[str, Any]:
    """解析 YAML frontmatter（第一对 --- 之间），yaml 失败降级行扫描。"""
    return memory_tree.parse_frontmatter(content)


def _list_memories(memory_dir: str, root_label: str = ".") -> list[dict[str, str]]:
    """列出记忆元信息（兼容布局：category/ 子目录 + 遗留扁平文件）"""
    memories: list[dict[str, str]] = []
    for leaf in memory_tree.leaves_in_dir(memory_dir):
        try:
            with open(leaf["path"], encoding="utf-8") as f:
                content = f.read()
        except Exception:
            continue
        fm = _parse_frontmatter(content)
        memories.append({
            "name": leaf["name"],
            "type": str(fm.get("type") or leaf["category"]),
            "description": str(fm.get("description") or ""),
            "created": str(fm.get("created") or ""),
            "updated": str(fm.get("updated") or "") or str(fm.get("created") or ""),
            "hint": str(fm.get("hint") or ""),
            "path": leaf["path"],
            "content": content,
            "root": root_label,
        })
    return memories


def _list_global_memories() -> list[dict[str, str]]:
    """合并三来源全局记忆（fetched → public → private，同名 private 胜出）。"""
    from fp_core.config import user_dirs

    merged: dict[str, dict[str, str]] = {}
    for d in user_dirs("memory"):
        for m in _list_memories(d, root_label="~"):
            merged[m["name"]] = m  # 后加载（更高优先级）覆盖
    return list(merged.values())


def _parse_memory_body(content: str) -> str:
    """从文件内容中提取正文（跳过 YAML frontmatter）"""
    lines = content.split("\n")
    body_start = 0
    if lines and lines[0].strip() == "---":
        for i in range(1, len(lines)):
            if lines[i].strip() == "---":
                body_start = i + 1
                break
    return "\n".join(lines[body_start:]).strip()


def _score_units(query: str) -> list[str]:
    """打分档查询单元：ASCII 词（≥2 字符）+ CJK 连续段的 bigram（单字段丢弃防噪声）。"""
    units: list[str] = [w for w in re.findall(r"[a-z0-9_\-]{2,}", query.lower())]
    for run in re.findall(r"[\u4e00-\u9fff]+", query):
        if len(run) >= 2:
            units.extend(run[i : i + 2] for i in range(len(run) - 1))
    return units


def _entry_label(m: dict[str, str]) -> str:
    """索引行条目：name 或 name→hint（hint 截断至 40 字符）"""
    hint = m.get("hint") or ""
    if hint:
        h = hint if len(hint) <= 40 else hint[:40] + "…"
        return f"{m['name']}→{h}"
    return m["name"]


def _stale_note(updated: str) -> str:
    """updated 距今超过 STALE_DAYS 天时返回过期提醒，解析失败/未超期 → 空串。"""
    try:
        dt = datetime.strptime(updated.strip(), "%Y-%m-%d %H:%M")
    except ValueError:
        return ""
    days = (datetime.now() - dt).days
    if days > STALE_DAYS:
        return f"⚠️ 记忆已 {days} 天未更新（> {STALE_DAYS} 天）。若涉及环境/权限/路径/端口，使用前请先验证是否仍有效。"
    return ""


_ESCAPE_FOOTER = (
    "├─ 未找到？升级路径：① memory_read(path='~/<类>') 浏览候选类 → "
    "② memory_read(query=换词/同义词) 跨类搜索 → "
    "③ memory_read(all=true) 全量扫描。全量扫描后仍无，才可报告「没有」。"
)


def _build_full_scan(global_memories: list[dict[str, str]], local_memories: list[dict[str, str]]) -> str:
    """逃生梯第 3 档：全部叶子的一行摘要（≈旧全量索引，但按需一次调用、不进前缀）"""
    total = len(global_memories) + len(local_memories)
    lines = [f"📋 全量记忆扫描（共 {total} 条，一行摘要）", ""]
    for m in global_memories:
        lines.append(f"~/{m['type']}/{_entry_label(m)} — {m['description']}")
    for m in local_memories:
        lines.append(f"./{m['type']}/{_entry_label(m)} — {m['description']}")
    if total == 0:
        lines.append("（记忆库为空）")
    else:
        lines.append("")
        lines.append("└─ 找到目标 → memory_read(name=...) 读正文；仍没有 → 该记忆可能不存在，用 memory_save 保存")
    return "\n".join(lines)


async def execute(params: dict[str, Any]) -> str:
    """读取记忆（异步）"""
    name: str = str(params.get("name") or "").strip()
    path: str = str(params.get("path") or "").strip()
    query: str = str(params.get("query") or "").strip()
    all_scan: bool = bool(params.get("all"))

    loop = asyncio.get_running_loop()
    local_dir = os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)

    # ── 入口⑤：all=true — 全量扫描（先于其它判断，与 name/path/query 可并存时优先级最低）──
    if all_scan and not any([name, path, query]):
        global_memories = await loop.run_in_executor(None, _list_global_memories)
        local_memories = await loop.run_in_executor(None, _list_memories, local_dir)
        return _build_full_scan(global_memories, local_memories)

    # ── 入口①：空参 → level-0 根索引（派生缓存，确保新鲜后读盘——手改优先）──
    if not any([name, path, query]):

        def _read_both() -> str:
            parts = [p for p in (memory_tree.read_index("~"), memory_tree.read_index(".")) if p]
            return "\n".join(parts)

        index = await loop.run_in_executor(None, _read_both)
        if index:
            return index
        return "（记忆库为空：全局与本地均无记忆）"

    # ── 入口②：memory_read(name="foo") — 精确读取 ──
    if name:
        global_memories = await loop.run_in_executor(None, _list_global_memories)
        local_memories = await loop.run_in_executor(None, _list_memories, local_dir)
        results = [m for m in global_memories + local_memories if m["name"] == name]

        if not results:
            hints = [m["name"] for m in global_memories + local_memories]
            close = [h for h in hints if name.lower() in h.lower() or h.lower() in name.lower()]
            msg = f"未找到名为「{name}」的记忆"
            if close:
                msg += f'，相似名称：{", ".join(close[:5])}（使用 memory_read(name="...") 精确读取）'
            return msg

        # 同名冲突：本地排后 → 本地胜
        selected = results[-1]
        await loop.run_in_executor(None, memory_tree.bump_access, selected["root"], selected["name"])
        body = _parse_memory_body(selected["content"])
        root_tag = "~" if selected["root"] == "~" else "📍 ./"
        header = f"[{root_tag}/{selected['type']}] {selected['name']} — {selected['description']}"
        lines = [f"📋 {header}"]
        if selected.get("updated"):
            lines.append(f"🕒 {selected['created'] or '?'} 创建 / {selected['updated']} 更新")
            note = _stale_note(selected["updated"])
            if note:
                lines.append(note)
        lines.append("")
        lines.append(body)
        return "\n".join(lines)

    # ── 入口③：memory_read(path="~/skill") — level-1 下钻 ──
    if path:
        path_match = re.match(r"^(~|\.)/(.+)$", path)
        if not path_match:
            return "path 格式错误。示例：memory_read(path='~/skill') 或 memory_read(path='./project')"

        root_char = path_match.group(1)
        category = path_match.group(2).strip().lower()
        root_label = "~" if root_char == "~" else "."

        def _read_level1() -> tuple[str, list[str]]:
            node = memory_tree.read_node(root_label, category)
            avail = sorted(c for c in memory_tree.categories(root_label) if not c.startswith("_"))
            return node, avail

        node, available = await loop.run_in_executor(None, _read_level1)
        if not node:
            msg = f"📂 {root_label}/{category}/ — 该分类下无记忆"
            if available:
                msg += f'\n可选分类：{"、".join(available)}（使用 memory_read(path="{root_label}/<分类>") 浏览）'
            else:
                msg += "\n（记忆库为空）"
            return msg
        return node

    # ── 入口④：memory_read(query="...") — FTS5 排序 + AND 子串兜底（双轨，裁决 Q3）──
    if query:
        global_memories = await loop.run_in_executor(None, _list_global_memories)
        local_memories = await loop.run_in_executor(None, _list_memories, local_dir)
        all_memories = global_memories + local_memories

        fts_results = await loop.run_in_executor(None, memory_index.search, query)

        if fts_results:
            results = fts_results
        else:
            # AND 子串兜底（短词 trigram 失效 / FTS miss 的保守路径——保持旧语义）
            keywords = [kw.lower() for kw in query.split()]

            def matches(m: dict[str, str]) -> bool:
                search_space = (
                    m["name"].lower()
                    + " "
                    + m["description"].lower()
                    + " "
                    + m["type"].lower()
                    + " "
                    + _parse_memory_body(m["content"]).lower()
                )
                return all(kw in search_space for kw in keywords)

            results = [m for m in all_memories if matches(m)]

        if not results:
            # ── 打分档（词云评审裁决：CJK 自然语言查询无空格分词时，FTS 短语与 AND
            #    子串双双失效。以 query 的 bigram/ASCII 词为单位做命中计数排序，
            #    score≥1 即入候选——ranked score（非概率），零词重叠则仍 miss）──
            units = _score_units(query)

            def _scored(m: dict[str, str]) -> int:
                hay = (
                    m["name"].lower()
                    + " "
                    + m["description"].lower()
                    + " "
                    + m["type"].lower()
                    + " "
                    + _parse_memory_body(m["content"]).lower()
                )
                return sum(1 for u in units if u in hay)

            scored = sorted(
                ((_scored(m), m) for m in all_memories),
                key=lambda t: t[0],
                reverse=True,
            )
            results = [m for s, m in scored if s > 0][:8]

        if not results:
            return f"📋 搜索「{query}」无匹配（词法零命中——这是已知天花板，非静默失败）\n{_ESCAPE_FOOTER}"

        lines = [f"📋 搜索「{query}」（共 {len(results)} 条，打分排序）", ""]
        for m in results:
            root_tag = "~" if m["root"] == "~" else "./"
            lines.append(f"{root_tag}{m['type']}/{m['name']} — {m['description']}")
        lines.append("")
        lines.append("└─ 点名读取 → memory_read(name=...)；结果不全 → memory_read(all=true) 全量扫描")
        return "\n".join(lines)

    return "⚠️ 参数异常（不应到达此处）"
