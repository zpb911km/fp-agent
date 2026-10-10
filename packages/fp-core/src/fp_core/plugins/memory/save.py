"""
Memory Save 插件 v3 — 树布局写入（设计：/tmp/fp_memory_tree_design_20261009.md）

布局： <mem_dir>/<category>/<name>.md（叶子=真相源，原子写）
写入侧职责：context 可选（lint 追踪缺失）、分类允许新词表（B 树分裂需要，词表外 lint 报告）、
写后派生缓存按需刷新（_node.md / _index.md 只在 stale 时重写——用户手改优先）+ FTS 旁路 upsert。
"""

import asyncio
import contextlib
import os
from datetime import datetime
from typing import Any, cast

from fp_core import config

from . import index as memory_index
from . import tree as memory_tree

# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "memory_save",
        "description": "保存一条长期记忆（跨会话持久化）。适合记住用户偏好、项目关键信息、重要决策等。",
        "parameters": {
            "type": "object",
            "properties": {
                "root": {
                    "type": "string",
                    "enum": ["~", "."],
                    "description": "存放位置：~ 全局（跨项目）/ . 本地（当前项目 .fp/ 下）",
                },
                "category": {
                    "type": "string",
                    "description": "分类名（如 skill, project, user, feedback 等）。禁用词：core, misc, other",
                },
                "name": {"type": "string", "description": "记忆名称，英文短词，全树唯一（跨类别不重名）"},
                "description": {
                    "type": "string",
                    "description": "一句话描述（脱离上下文也自包含，禁用「这个/上述/刚才」）",
                },
                "content": {"type": "string", "description": "记忆正文内容"},
                "context": {
                    "type": "string",
                    "description": "何时/何地/何种范围成立（一行）。强烈建议提供——缺它换个会话就不知所云。",
                },
                "hint": {
                    "type": "string",
                    "description": "可选：路径/命令等短硬事实（≤40 字符），写入 frontmatter 并可升入根索引热键。",
                },
            },
            "required": ["root", "category", "name", "description", "content"],
        },
    },
}


async def execute(params: dict[str, Any]) -> str:
    """保存记忆（异步）"""
    root = params.get("root", "")
    category = str(params.get("category") or "").strip().lower()
    name = params.get("name", "").strip()
    description = params.get("description", "").strip()
    content = params.get("content", "")
    context = str(params.get("context") or "").strip()

    # ── 验证必填 ──
    if not all([root, category, name, description, content]):
        raise ValueError("memory_save 需要以下参数：root, category, name, description, content")

    # ── 验证 root ──
    if root not in ("~", "."):
        raise ValueError("root 必须是 '~'（全局）或 '.'（本地）")

    # ── 验证分类（裁决 Q2：允许新分类——B 树分裂需要；仅拦禁用词，词表外交给 lint）──
    if category in config.FORBIDDEN_CATEGORIES:
        raise ValueError(
            f"分类名 '{category}' 被禁用（{', '.join(sorted(config.FORBIDDEN_CATEGORIES))}），请使用有具体语义的分类名"
        )

    # ── 确定物理目录（树布局：全局=三来源最高优先级 private/memory，本地=项目 .fp/memory）──
    root_label = "~" if root == "~" else "."
    base_dir = config.user_dirs("memory")[-1] if root == "~" else os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)
    memory_dir = os.path.join(base_dir, category)

    # ── 安全文件名 ──
    safe_name = name.replace(" ", "_").replace("/", "_")
    path = os.path.join(memory_dir, f"{safe_name}.md")
    legacy_flat = os.path.join(base_dir, f"{safe_name}.md")  # 迁移前的遗留扁平位置
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    hint = str(params.get("hint") or "").strip()[:80]

    loop = asyncio.get_running_loop()

    def _read_old_meta() -> dict[str, Any]:
        """读既有 frontmatter（保 created）：优先目标位置，其次同分类遗留扁平文件。"""
        import yaml

        for candidate in (path, legacy_flat):
            try:
                with open(candidate, encoding="utf-8") as f:
                    text = f.read()
            except OSError:
                continue
            lines = text.split("\n")
            if not lines or lines[0].strip() != "---":
                continue
            end = 1
            while end < len(lines) and lines[end].strip() != "---":
                end += 1
            if end >= len(lines):
                continue
            try:
                fm_raw = yaml.safe_load("\n".join(lines[1:end]))
                if isinstance(fm_raw, dict):
                    fm = cast(dict[str, Any], fm_raw)
                    if str(fm.get("type") or "").strip().lower() != category:
                        continue  # 遗留文件属于别的分类 → 不是同一条记忆，不继承
                    return fm
            except Exception:
                continue
        return {}

    def _write():
        old = _read_old_meta()
        created = old.get("created") if isinstance(old.get("created"), str) and old["created"].strip() else date
        os.makedirs(memory_dir, exist_ok=True)
        # 原子写（temp + os.replace，同目录保证同文件系统）：
        # 同 cwd 多实例共写时，读者永远只见完整文件。跨实例 last-writer-wins 由锁+journal 演进解决。
        tmp_path = f"{path}.{os.getpid()}.tmp"
        try:
            with open(tmp_path, "w", encoding="utf-8") as f:
                f.write("---\n")
                f.write(f"name: {safe_name}\n")
                f.write(f"description: {description}\n")
                f.write(f"type: {category}\n")
                f.write(f"created: {created}\n")
                f.write(f"updated: {date}\n")
                if context:
                    f.write(f"context: {context}\n")
                if hint:
                    f.write(f"hint: {hint}\n")
                f.write("---\n\n")
                f.write(content + "\n")
            os.replace(tmp_path, path)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp_path)
            raise
        # 遗留扁平同名文件已迁移到树布局 → 移除旧位置（同一分类才算同一条记忆）
        if legacy_flat != path and os.path.isfile(legacy_flat):
            with contextlib.suppress(OSError):
                os.unlink(legacy_flat)

    await loop.run_in_executor(None, _write)

    # ── 派生缓存刷新（stale 才重写——用户手改优先）+ FTS 旁路 upsert ──
    def _refresh():
        memory_tree.refresh_node(root_label, category)
        memory_tree.refresh_index(root_label)
        leaf = {
            "name": safe_name,
            "root": root_label,
            "category": category,
            "path": path,
            "mtime": str(os.path.getmtime(path)),
        }
        doc = "\n".join([safe_name, description, category, hint, context, content])
        memory_index.upsert(leaf, doc)

    await loop.run_in_executor(None, _refresh)

    root_tag = "~（全局）" if root == "~" else ".（本地）"
    return f"✅ 记忆已保存 [{root_tag}/{category}] {safe_name}"
