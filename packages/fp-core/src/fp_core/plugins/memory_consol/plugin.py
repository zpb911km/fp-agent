"""memory_consol — 记忆巩固检测插件（P4 接线）

ON_INITIALIZED（初始化检查通过）时触发一次 `memory_consolidate.run()`：
  - 日门控：同一天只跑一次（跨会话/多次重启幂等），force 由测试直接调核心模块
  - flock 单写者：同 cwd 多实例并发时仅一个执行，其余立即跳过
  - 结果三出口：journal jsonl、dated 报告 md、日志摘要

全部检测逻辑在 `fp_core.memory_consolidate`（纯函数，可独立测试），
本插件只做注册与装配（文件即开关：memory_consol → 重命名为
memory_consol.disabled 即停用）。
"""

from typing import Any

from fp_core.core.lifecycle import HookContext, LifecycleHook, LifecycleManager
from fp_core.logger import get_logger
from fp_core.plugins.base.plugin import Plugin, PluginConfig

__all__ = ["MemoryConsolPlugin"]


class MemoryConsolPlugin(Plugin):
    """记忆巩固检测（日频机检 + journal + 报告）"""

    name = "memory_consol"
    version = "1.0.0"

    def __init__(self, config: PluginConfig | None = None):
        super().__init__(config)
        self._ran = False

    def on_register(self, lifecycle: LifecycleManager) -> None:
        lifecycle.register(
            LifecycleHook.ON_INITIALIZED,
            self._on_initialized,
            priority=50,
            name="memory_consol_run",
        )

    def on_unregister(self) -> None:
        self._ran = False

    async def _on_initialized(self, ctx: HookContext, **kwargs: Any) -> HookContext:
        if self._ran:
            return ctx
        self._ran = True
        import asyncio

        from fp_core.memory_consolidate import run

        log = get_logger()
        try:
            summary = await asyncio.to_thread(run)
            log.info(f"[memory_consol] {summary}")
        except Exception as e:  # 检测失败绝不阻塞初始化
            log.warning(f"[memory_consol] 巩固检测失败（不影响启动）: {e}")
        return ctx
