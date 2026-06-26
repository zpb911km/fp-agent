"""exit! 命令 — 核弹级退出：删除当前会话、不留痕迹"""

import os

name = "exit!"
aliases = []
description = "核弹级退出：删除当前会话、不留痕迹"


def execute(agent, arg: str) -> tuple[bool, str]:
    sid = agent.session.session_id
    path = agent.session.get_session_path()

    # 标记核弹退出 — shutdown 时会删除会话文件
    agent.set_nuclear_exit()

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
