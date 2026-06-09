"""back 命令 — 回退到对话的某个历史时刻（异步）

支持参数传递：
  /back              → 交互模式，显示列表让用户选择
  /back <index>      → 直接回溯到指定位置，删除后续消息
  /back <index> 1    → 直接回溯到指定位置，保留后续消息
  /back <index> 2    → 直接回溯到指定位置，删除后续消息
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
            if mode not in (1, 2):
                msg = f"❌ 无效模式：{mode}，请输入 1（保留）或 2（删除）"
                agent.io.error(msg)
                return (True, msg)
        except ValueError:
            msg = f"❌ 无效参数：'{parts[1]}' 不是数字"
            agent.io.error(msg)
            return (True, msg)
    
    result = await agent.back(target_idx=index, mode=mode)
    return (True, result)
