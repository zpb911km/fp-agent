"""exit! 命令 — 核弹级退出：删除当前会话、不留痕迹"""

import os
import sys

import display


name = "exit!"
aliases = []
description = "核弹级退出：删除当前会话、不留痕迹"


def execute(agent, arg: str) -> bool:
    sid = agent.session.session_id
    path = agent.session._session_path()
    
    # 标记核弹退出，让 shutdown 跳过保存
    agent._nuclear_exit = True
    
    # 删除会话文件
    if os.path.exists(path):
        try:
            os.remove(path)
            display.info(f"💥 会话 {sid} 已删除，不留痕迹")
        except Exception as e:
            display.warning(f"⚠️  删除失败: {e}")
    
    raise SystemExit()
