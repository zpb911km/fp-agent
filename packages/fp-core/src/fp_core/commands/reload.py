"""
/reload 命令 — 热重载 Agent

重新加载所有核心模块并创建新 Agent 实例（不重启进程）。
会话上下文自动保留。

用法:
  /reload           — 完整热重载
  /reload modules   — 仅重载模块，不创建新 Agent（调试用）

工作方式:
  1. 保存当前会话上下文
  2. shutdown 旧 Agent（释放连接池 + 清理钩子）
  3. importlib.reload 所有核心模块（按依赖顺序）
  4. 创建新 Agent 实例（自动扫描内置+用户插件/工具）
  5. 恢复旧会话
  6. 将新 Agent 存入 state._reload_result，外层循环消费后交换引用
"""

import time

from fp_core.core.state import State

name = "reload"
aliases = ["rl", "hotreload"]
description = "热重载 Agent（重新加载核心模块，保留会话上下文）"


async def execute(state: State, arg: str) -> tuple[bool, str]:
    """执行热重载"""
    from fp_core.core.reloader import AgentReloader

    arg = arg.strip()

    # ── 仅重载模块（调试用） ──
    if arg == "modules":
        try:
            AgentReloader.reload_modules()
            return (True, "✅ 核心模块已重载（未重建 Agent）")
        except RuntimeError as e:
            return (True, f"❌ 模块重载失败: {e}")

    # ── 检查是否正在处理 ──
    if state.agent is not None and state.agent.is_processing:
        return (True, "❌ Agent 正在处理请求，请稍后重试")

    t0 = time.time()

    # ── 从旧 agent 提取 IO 通道和 shutdown 回调，传给新 Agent ──
    old_io = state.io
    old_shutdown = getattr(state.agent, "_shutdown_callback", None) if state.agent else None

    try:
        new_agent, info = await AgentReloader.reload(state.agent, io=old_io, on_shutdown=old_shutdown)
    except RuntimeError as e:
        return (True, f"❌ 重载失败: {e}")

    elapsed = time.time() - t0

    # ── 暂存新 Agent，供外层循环消费 ──
    state._reload_result = (new_agent, info)  # pyright: ignore[reportPrivateUsage]  # noqa: E501 设计内跨类协议（State 注释明确该字段专供 reload 命令使用）

    session_status = "✅ 已恢复" if info.get("session_restored") else "⚠️ 新建会话"
    lines = [
        f"🔄 **热重载完成**（{elapsed:.1f}s）",
        "",
        "| 项目 | 值 |",
        "|------|-----|",
        f"| 模型 | `{info['model']}` |",
        f"| 会话 | `{info['session_id']}` {session_status} |",
        "",
    ]
    return (True, "\n".join(lines))
