"""token 命令 — 显示当前会话的 token 消耗统计

通过 state.token_tracker 读取实时累计数据。
"""

from fp_core.core.state import State

name = "token"
aliases: list[str] = ["tokens", "usage"]
description = "显示当前会话的 token 消耗统计"


def execute(state: State, arg: str) -> tuple[bool, str]:
    """显示 token 消耗统计"""
    tracker = getattr(state, "token_tracker", None)
    if tracker is None:
        return (True, "⚠️  Token 跟踪器不可用")

    current_model = state.model_name if hasattr(state, "model_name") else ""
    output = tracker.format_detailed(current_model=current_model)

    return (True, output)
