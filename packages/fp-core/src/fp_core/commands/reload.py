"""
/reload 命令 — 热重启（命令面，人触发、豁免两段式口令）

与工具面 reload 共用同一激活核心 fp_core.core.handoff.perform_exec_reload：
  落盘会话 → 写 handoff(kind=command) → os.execve 按原启动命令重启
新实例恢复会话后由 consume_reload_handoff 置 state._reload_notice，
入口显示一行完成提示即回到正常输入（命令发生在 process 入口，无挂起
工具轮，不进续接）。执行到 execve 即止、永不返回；失败返回错误文本，
当前实例无损（救火原则）。

历史：曾走 AgentReloader.reload 原地重建（shutdown 旧 → importlib.reload →
新建）；该路径失败会留下已 shutdown 的僵尸 Agent，已废弃删除。

用法:
  /reload           — 进程级热重启
  /reload modules   — 仅重载模块，不创建新 Agent（调试用，原地 importlib.reload）
"""

from fp_core.core.state import State

name = "reload"
aliases = ["rl", "hotreload"]
description = "热重启进程（落盘会话 → execve 原启动命令重启，会话自动恢复）"


async def execute(state: State, arg: str) -> tuple[bool, str]:
    """执行热重启"""
    arg = arg.strip()

    # ── 仅重载模块（调试用） ──
    if arg == "modules":
        from fp_core.core.reloader import AgentReloader

        try:
            AgentReloader.reload_modules()
            return (True, "✅ 核心模块已重载（未重建 Agent）")
        except RuntimeError as e:
            return (True, f"❌ 模块重载失败: {e}")

    # ── 检查是否正在处理 ──
    if state.agent is not None and state.agent.is_processing:
        return (True, "❌ Agent 正在处理请求，请稍后重试")

    from fp_core.core.handoff import perform_exec_reload

    # 成功路径 execve 不返回；能返回必是失败（核心内已回滚）。
    err = await perform_exec_reload(state, kind="command")
    return (True, err)
