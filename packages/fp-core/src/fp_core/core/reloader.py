"""
AgentReloader — 模块重载引擎（原地 importlib.reload）

只保留 `/reload modules` 的调试路径：按依赖顺序刷新核心模块代码，
不换 Agent 实例、不碰会话。不关心具体前端（CLI / WebUI / ACP）。

进程级热重启（落盘 → execve 按原启动命令重启）已统一到
fp_core.core.handoff.perform_exec_reload（工具面 reload 与命令面 /reload
共用）。原先的 AgentReloader.reload 原地重建路径（shutdown 旧 Agent →
importlib.reload → 新建）已废弃删除——它在失败时会留下已 shutdown 的
僵尸 Agent（自杀窗口），与 reload 门禁协议的救火原则冲突。

不处理：
  - 前端通知（EventBus / WebSocket 等由调用方处理）
  - 激活门禁（两段式口令在工具层 reload_plugin，命令面由人触发豁免）
"""

import importlib
import sys

# ── 模块重载列表（按依赖顺序） ────────────────────────────────
# 第 1 层：无项目内部依赖
# 第 2 层：依赖 config
# 第 3 层：依赖 core.*
# 第 4 层：命令/工具注册表（含全局状态）
# 第 5 层：Agent 主干（依赖以上所有）

_RELOAD_MODULES: list[str] = [
    # Layer 1
    "fp_core.config",
    # Layer 2
    "fp_core.core.io",
    "fp_core.core.lifecycle",
    "fp_core.core.session",
    "fp_core.core.llm_client",
    # Layer 3
    "fp_core.plugins.base.plugin",
    "fp_core.prompts.agent",
    # Layer 4
    "fp_core.commands",
    "fp_core.tools",
    # Layer 5
    "fp_core.core.agent",
]


def _reload_modules() -> None:
    """按依赖顺序刷新所有核心模块。

    先 reload 各包的非主模块（子模块），
    再按 _RELOAD_MODULES 顺序 reload 主模块，
    确保父模块 import 时拿到的是已刷新的子模块代码。
    """
    reloaded_subs: set[str] = set()

    # ── 先 reload 子模块（排除主模块列表中的） ──
    for prefix in ("fp_core.tools.", "fp_core.commands.", "fp_core.core."):
        for mod_name in list(sys.modules.keys()):
            if (
                mod_name.startswith(prefix)
                and mod_name in sys.modules
                and mod_name not in _RELOAD_MODULES
                and mod_name not in reloaded_subs
            ):
                importlib.reload(sys.modules[mod_name])
                reloaded_subs.add(mod_name)

    # ── 按依赖顺序 reload 主模块 ──
    for mod_name in _RELOAD_MODULES:
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception as e:
                raise RuntimeError(f"重载模块 {mod_name} 失败: {e}") from e

    importlib.invalidate_caches()


class AgentReloader:
    """模块重载器（纯逻辑，无 UI 绑定）。

    典型用法（调试/命令面 `/reload modules`）::

        from fp_core.core.reloader import AgentReloader

        AgentReloader.reload_modules()
    """

    @staticmethod
    def reload_modules() -> None:
        """仅刷新模块代码，不创建 Agent。

        适用于需要手动控制 Agent 创建时机的场景。
        """
        _reload_modules()
