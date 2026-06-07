"""
Delete Empty Sessions 插件 — 清理记录为空的会话文件

判断标准：读取文件后，仅有 meta 行（第一行），后续无任何实际消息记录。
不依赖 meta 中的 message_count 字段（防止 bug 误判），而是逐行实际计数。
"""

import asyncio
import json
import os
from typing import Any, Dict

import config


# ── 插件定义（OpenAI function calling schema） ──────────────────────

PLUGIN_DEFINITION = {
    "type": "function",
    "function": {
        "name": "delete_empty_sessions",
        "description": "扫描并清理所有记录为空（仅有 meta 行、无实际消息记录）的会话文件。通过逐行计数而非依赖 meta 字段判断，防止 bug 误判。支持 dry-run 预览模式。",
        "parameters": {
            "type": "object",
            "properties": {
                "dry_run": {
                    "type": "boolean",
                    "description": "设为 true 则仅预览不执行删除，列出所有将被清理的文件（默认 false）",
                },
            },
            "required": [],
        },
    },
}


def _is_meta_line(line: str) -> bool:
    """检查一行是否为有效的 meta 行"""
    try:
        obj = json.loads(line)
        return isinstance(obj, dict) and obj.get("__meta__") is True
    except (json.JSONDecodeError, ValueError):
        return False


def _analyze_file(filepath: str) -> dict:
    """
    分析单个会话文件。
    
    返回:
        {"valid": bool, "empty": bool, "message_count": int, "reason": str}
    """
    filename = os.path.basename(filepath)

    try:
        with open(filepath, "rb") as f:
            raw = f.read()
    except PermissionError:
        return {"valid": False, "empty": False, "message_count": -1,
                "reason": f"⚠️ {filename}: 无读取权限，跳过"}

    # 0 字节文件 → 空会话
    if len(raw) == 0:
        return {"valid": True, "empty": True, "message_count": 0,
                "reason": f"  🗑️ {filename}: 0字节文件"}

    # 尝试按文本解码
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        return {"valid": False, "empty": False, "message_count": -1,
                "reason": f"  🔒 {filename}: 非UTF-8编码，保留"}

    lines = text.splitlines()

    # 必须至少有一行（meta 行）
    if not lines:
        return {"valid": True, "empty": True, "message_count": 0,
                "reason": f"  🗑️ {filename}: 仅含空行"}

    # 第一行必须是有效的 meta
    if not _is_meta_line(lines[0]):
        return {"valid": False, "empty": False, "message_count": -1,
                "reason": f"  🔒 {filename}: 首行非 meta 或 JSON 解析失败，保留"}

    # 逐行统计 meta 之后的有效内容行数（跳过空行）
    message_count = 0
    for line in lines[1:]:
        if line.strip():
            message_count += 1

    if message_count == 0:
        return {"valid": True, "empty": True, "message_count": 0,
                "reason": f"  🗑️ {filename}: 仅有meta行，无消息记录"}
    else:
        return {"valid": True, "empty": False, "message_count": message_count,
                "reason": f"  ✓ {filename}: {message_count} 条消息"}


async def execute(params: Dict[str, Any]) -> str:
    """
    扫描并清理空会话文件（异步）
    
    Args:
        params: 可选包含 'dry_run' 键
        
    Returns:
        清理报告
    """
    dry_run = params.get("dry_run", False)
    sessions_dir = getattr(config, "SESSIONS_DIR", None)

    if not sessions_dir or not os.path.isdir(sessions_dir):
        return f"错误：会话目录不存在或未配置 ({sessions_dir})"

    # 扫描所有 jsonl 文件
    all_files = sorted(os.listdir(sessions_dir))
    jsonl_files = [f for f in all_files if f.endswith(".jsonl")]

    deleted = []
    kept = []
    errors = []

    for filename in jsonl_files:
        filepath = os.path.join(sessions_dir, filename)
        result = _analyze_file(filepath)

        if result["empty"]:
            deleted.append(result["reason"])
        elif result["valid"]:
            kept.append(result["reason"])
        else:
            # valid=False 表示无法安全判断，保留
            errors.append(result["reason"])

    # ── 执行删除（非 dry-run 模式） ──
    if not dry_run:
        for filename in jsonl_files:
            filepath = os.path.join(sessions_dir, filename)
            result = _analyze_file(filepath)
            if result["empty"]:
                try:
                    os.remove(filepath)
                except Exception as e:
                    errors.append(f"  ⚠️ {filename}: 删除失败 - {e}")

    # ── 生成报告 ──
    action = "🔍 预览" if dry_run else "🧹 清理"
    lines = [f"{action}完成 — 共扫描 {len(jsonl_files)} 个会话文件"]

    if deleted:
        lines.append(f"\n🗑️ 空会话 ({len(deleted)} 个，将{'被删除' if dry_run else '已删除'}):")
        lines.extend(deleted)

    if kept:
        lines.append(f"\n🔒 有效会话 ({len(kept)} 个，保留):")
        lines.extend(kept)

    if errors:
        lines.append(f"\n⚠️ 无法判断/错误 ({len(errors)} 个，已保留):")
        lines.extend(errors)

    if not deleted and not errors:
        lines.append("\n✅ 没有需要清理的空会话文件")

    if dry_run and deleted:
        lines.append(f"\n💡 如确认无误，再次执行不加 dry_run 参数即可执行删除")

    return "\n".join(lines)
