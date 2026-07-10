"""exit! 命令 — 核弹级退出：删除当前会话、不留痕迹

直接操作 state.session + 设置 state.nuclear_exit。
"""

import os

name = "exit!"
aliases = []
description = "核弹级退出：删除当前会话、不留痕迹"


def execute(state, arg: str) -> tuple[bool, str]:
    sid = state.session_id
    path = state.session.get_session_path()

    # 标记核弹退出 — shutdown 时会删除会话文件
    state.nuclear_exit = True

    # 提前删除文件（shutdown 也会删，双重保险）
    if os.path.exists(path):
        try:
            os.remove(path)
            msg = f"💥 会话 {sid} 已删除，不留痕迹"
        except Exception as e:
            msg = f"⚠️  删除失败: {e}"
    else:
        msg = ""

    raise SystemExit(msg)
