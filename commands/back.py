"""back 命令 — 回退到对话的某个历史时刻（异步）

支持参数传递：
  /back              → 交互模式，显示列表让用户选择
  /back <index>      → 直接回溯到指定位置，删除后续消息
  /back <index> 2    → 直接回溯到指定位置，删除后续消息
  /back <index> 1    → ❌ 暂不支持（保留后续消息），请使用 /fork
"""

name = "back"
aliases = []
description = "回退到对话的某个历史时刻"


async def execute(agent, arg: str) -> tuple[bool, str]:
    # 解析参数
    parts = arg.strip().split()
    index = None
    mode = None

    if parts:
        try:
            index = int(parts[0])
        except ValueError:
            msg = f"❌ 无效参数：'{parts[0]}' 不是数字"
            agent.io.error(msg)
            return (True, msg)

    if len(parts) >= 2:
        try:
            mode = int(parts[1])
            if mode != 2:
                msg = "❌ mode=1（保留后续消息）暂不支持，请用 mode=2 或 /fork"
                agent.io.error(msg)
                return (True, msg)
        except ValueError:
            msg = f"❌ 无效参数：'{parts[1]}' 不是数字"
            agent.io.error(msg)
            return (True, msg)

    result = await agent.back(target_idx=index, mode=mode)
    return (True, result)
