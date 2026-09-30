"""
ToolExecutor — 工具执行层

职责：
- 持有一个 ToolRegistry 实例
- 提供 get_definitions() 返回 OpenAI schema（注入全工具可选 background 参数）
- 提供 execute(tool_call) 执行工具调用
- 框架层后台化：background=true 立即移交后台返回 job 收据；
  默认前台阻塞，但超过 FW_BG_TIMEOUT（默认 10s）自动转后台返回 job_id
  （自管理工具 SELF_MANAGED — bash/ask_user/wait_job 等 — 豁免，语义见设计 §0）
- 不处理 IO、不显示工具调用信息
- 只做"工具调用→结果"的纯执行
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
from typing import TYPE_CHECKING, Any, cast

from fp_core.core import jobs as jobs_impl

if TYPE_CHECKING:
    from fp_core.tools import ToolRegistry
    from fp_core.tools.core import OpenAISchema

# 框架层后台移交超时：前台超过该秒数 → 自动转后台并返回 job_id 收据。
# 置 0 或负数 = 关闭框架超时（全部前台阻塞，直到工具自身超时语义触发）。
FW_BG_TIMEOUT = float(os.environ.get("FP_TOOL_BG_TIMEOUT", "10"))

# 自管理工具：自带超时/后台语义，或语义上不可后台（控制面/等人/进程替换）。
# 框架层不拦 background 参数、不施加 FW_BG_TIMEOUT —— bash 的 on_timeout
# 已是同款机制；ask_user/wait_job 阻塞是设计本意；reload 是 exec 语义。
SELF_MANAGED: frozenset[str] = frozenset({
    "bash",
    "ask_user",
    "wait_job",
    "background",
    "kill_job",
    "list_jobs",
    "reload",
})


class ToolExecutor:
    """工具执行器"""

    def __init__(self, registry: ToolRegistry | None = None):
        """
        Args:
            registry: ToolRegistry 实例。None 时创建独立实例（不再共享全局单例）
        """
        if registry is not None:
            self._registry = registry
        else:
            from fp_core.tools import create_registry

            self._registry = create_registry()

    @property
    def registry(self) -> ToolRegistry:
        """工具注册表（供插件注册工具时使用）"""
        return self._registry

    def get_definitions(self) -> list[OpenAISchema]:
        """获取所有工具的 OpenAI function calling schema 列表

        幂等地注入全工具可选参数 `background`（自管理工具 SELF_MANAGED 除外；
        bash 已自带同名参数故天然跳过）—— 这是"全工具后台化"的 schema 面。
        """
        definitions = self._registry.get_all_definitions()
        for d in definitions:
            props = d["function"]["parameters"]["properties"]
            if d["function"]["name"] in SELF_MANAGED or "background" in props:
                continue
            props["background"] = {
                "type": "boolean",
                "description": (
                    "设为 true 立即移交后台执行并返回 job_id 收据"
                    "（结果落盘，wait_job 精确等待，完成后上下文自动注入完成通知）；"
                    "默认 false=前台阻塞执行（超过 10s 也会自动转后台并返回 job_id）"
                ),
            }
        return definitions

    async def execute(self, tool_call: dict[str, Any]) -> str:
        """
        执行工具调用。

        异常策略：不吞异常，所有工具执行异常向上传播。
        由 Agent 层的 _execute_one_tool 捕获并触发 ON_TOOL_ERROR 生命周期。

        Args:
            tool_call: tool_call dict，格式:
                {"id": "...", "type": "function",
                 "function": {"name": "...", "arguments": "..."}}

        Returns:
            执行结果的字符串表示

        Raises:
            json.JSONDecodeError: 工具参数 JSON 解析失败
            TypeError: 工具参数类型错误
            Exception: 工具执行时的其他异常
        """
        name: str = tool_call["function"]["name"]
        args: dict[str, Any] = json.loads(tool_call["function"]["arguments"])

        # ── 自管理工具：原样阻塞执行（自带超时/后台语义） ──
        if name in SELF_MANAGED:
            result = cast("str | None", await self._registry.execute(name, args))
            return str(result) if result is not None else "执行成功（无返回）"

        # ── background=true → 立即移交后台，返回 job 收据（框架层统一入口） ──
        bg_raw = args.pop("background", None)
        if str(bg_raw).lower() in ("true", "1", "yes"):
            job = jobs_impl.start_job(
                label=f"工具 {name}",
                coro=self._registry.execute(name, args),
            )
            return json.dumps(
                {
                    "status": "backgrounded",
                    "tool": name,
                    "job_id": job.id,
                    "result_file": job.result_path,
                    "note": (
                        "任务已在后台执行。完成后上下文会自动注入完成通知；"
                        "也可 wait_job 精确等待，kill_job 终止，list_jobs 查看。"
                    ),
                },
                ensure_ascii=False,
            )

        # ── 前台阻塞，但超过 FW_BG_TIMEOUT → 自动转后台返回 job_id ──
        timeout = FW_BG_TIMEOUT if FW_BG_TIMEOUT > 0 else None
        task = asyncio.get_running_loop().create_task(self._registry.execute(name, args))
        try:
            done, _ = await asyncio.wait({task}, timeout=timeout)
        except (KeyboardInterrupt, asyncio.CancelledError):
            # 用户中断：工具任务一并取消（传播进协程做清理），不留悬挂 job
            task.cancel()
            with contextlib.suppress(BaseException):
                await task
            raise
        if not done:
            job = jobs_impl.adopt_task(label=f"工具 {name}", task=task)
            return json.dumps(
                {
                    "status": "backgrounded_on_timeout",
                    "tool": name,
                    "job_id": job.id,
                    "result_file": job.result_path,
                    "note": (
                        f"前台等待 {FW_BG_TIMEOUT:g}s 超时，已自动转后台执行；"
                        "wait_job 拉取结果，kill_job 终止，list_jobs 查看。"
                    ),
                },
                ensure_ascii=False,
            )
        if task.cancelled():
            raise asyncio.CancelledError
        exc = task.exception()
        if exc is not None:
            raise exc
        result = cast("str | None", task.result())
        return str(result) if result is not None else "执行成功（无返回）"
