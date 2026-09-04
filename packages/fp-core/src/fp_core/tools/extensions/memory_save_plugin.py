"""
Memory Save 插件 v2 — 保存长期记忆（异步版本）

新架构：两棵根树（~/ 全局 + ./ 本地），三层深度（根→分类→叶子）
参数：root, category, name, description, content
"""

import asyncio
import os
from datetime import datetime
from typing import Any

from fp_core import config

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
                "name": {"type": "string", "description": "记忆名称，英文短词，在同一分类下唯一"},
                "description": {"type": "string", "description": "一句话描述"},
                "content": {"type": "string", "description": "记忆正文内容"},
                "hint": {
                    "type": "string",
                    "description": "可选：路径/命令等短硬事实（≤40 字符），写入 frontmatter 并在顶层索引行内联展示。",
                },
            },
            "required": ["root", "category", "name", "description", "content"],
        },
    },
}


async def execute(params: dict[str, Any]) -> str:
    """
    保存记忆（异步）

    Args:
        params: 包含 root, category, name, description, content 的字典

    Returns:
        保存结果
    """
    root = params.get("root", "")
    category = params.get("category", "").strip().lower()
    name = params.get("name", "").strip()
    description = params.get("description", "").strip()
    content = params.get("content", "")

    # ── 验证必填 ──
    if not all([root, category, name, description, content]):
        raise ValueError("memory_save 需要以下参数：root, category, name, description, content")

    # ── 验证 root ──
    if root not in ("~", "."):
        raise ValueError("root 必须是 '~'（全局）或 '.'（本地）")

    # ── 验证分类 ──
    if category in config.FORBIDDEN_CATEGORIES:
        raise ValueError(
            f"分类名 '{category}' 被禁用（{', '.join(sorted(config.FORBIDDEN_CATEGORIES))}），请使用有具体语义的分类名"
        )

    # ── 确定物理目录 ──
    # 全局（root="~"）保存到三来源最高优先级 private/memory；
    # 本地（root="."）保存到项目内 .fp/memory
    memory_dir = config.user_dirs("memory")[-1] if root == "~" else os.path.join(os.getcwd(), config.MEMORY_DIR_LOCAL)

    # ── 安全文件名 ──
    safe_name = name.replace(" ", "_").replace("/", "_")
    path = os.path.join(memory_dir, f"{safe_name}.md")
    date = datetime.now().strftime("%Y-%m-%d %H:%M")
    hint = str(params.get("hint") or "").strip()[:80]

    loop = asyncio.get_running_loop()

    def _read_old_meta() -> dict[str, Any]:
        """读取既有文件 frontmatter（保留原 created）；文件不存在/损坏 → {}。"""
        try:
            with open(path, encoding="utf-8") as f:
                content = f.read()
        except OSError:
            return {}
        lines = content.split("\n")
        if not lines or lines[0].strip() != "---":
            return {}
        end = 1
        while end < len(lines) and lines[end].strip() != "---":
            end += 1
        if end >= len(lines):
            return {}
        import yaml

        try:
            fm = yaml.safe_load("\n".join(lines[1:end]))
            return fm if isinstance(fm, dict) else {}
        except Exception:
            return {}

    def _write():
        old = _read_old_meta()
        created = old.get("created") if isinstance(old.get("created"), str) and old["created"].strip() else date
        os.makedirs(memory_dir, exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            f.write("---\n")
            f.write(f"name: {safe_name}\n")
            f.write(f"description: {description}\n")
            f.write(f"type: {category}\n")
            f.write(f"created: {created}\n")
            f.write(f"updated: {date}\n")
            if hint:
                f.write(f"hint: {hint}\n")
            f.write("---\n\n")
            f.write(content + "\n")

    await loop.run_in_executor(None, _write)

    root_label = "~（全局）" if root == "~" else ".（本地）"
    return f"✅ 记忆已保存 [{root_label}/{category}] {safe_name}"
