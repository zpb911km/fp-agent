"""exit! 命令 — 核弹级退出：删除当前会话、不留痕迹

只设置 nuclear_exit 标记，shutdown 时由 Agent 统一清理。
"""

from fp_core.core.state import State

name = "exit!"
aliases: list[str] = []
description = "核弹级退出：删除当前会话、不留痕迹"


def execute(state: State, arg: str) -> tuple[bool, str]:
    state.nuclear_exit = True
    raise SystemExit(f"💥 会话 {state.session_id} 已标记删除，即将销毁")
