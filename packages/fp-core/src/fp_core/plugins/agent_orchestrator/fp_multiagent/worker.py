"""worker 生成 — 复用 subagent 子进程机制（含 role 传递）

复用点：入口 `python -m fp_cli.main -m <query>`、环境变量守卫（FP_IS_SUBAGENT
拒绝递归）、父进程预生成 SID（FP_SUBAGENT_SID/PARENT_SID，失败可兜底补 meta）。

增强点（相对核心 subagent_plugin）：
- FP_SUBAGENT_ROLE：把 AgentRole（JSON）传入子进程，fp_cli 还原为 duck-typed 角色
- stdin=DEVNULL：worker 不继承父终端 stdin（避免 worker 内交互工具挂起）
- 结构化返回：{status, result, error, duration, sid}，供编排器记账
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from typing import Any

from fp_core.core.session import (  # pyright: ignore[reportPrivateUsage]
    _generate_sid,
    get_current_session_id,
)


async def spawn_worker(
    *,
    task: str,
    cwd: str,
    context: str = "",
    role_json: str = "",
    timeout: int = 300,
    extra_env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """启动一个 worker 子进程并等待其完成。

    Returns:
        {"status": "ok"|"warning"|"error", "result": str, "error": str,
         "duration": float, "sid": str}
    """
    query = f"[背景信息]\n{context}\n\n[任务]\n{task}" if context else task
    entry = [sys.executable, "-m", "fp_cli.main", "-m"]

    sub_sid = _generate_sid()
    parent_sid = get_current_session_id() or ""

    env = os.environ.copy()
    env["FP_IS_SUBAGENT"] = "1"
    env["FP_SUBAGENT_QUIET"] = "1"
    env["FP_SUBAGENT_SILENT"] = "1"
    env["FP_SUBAGENT_SID"] = sub_sid
    if parent_sid:
        env["FP_SUBAGENT_PARENT_SID"] = parent_sid
    if role_json:
        env["FP_SUBAGENT_ROLE"] = role_json
    if extra_env:
        env.update(extra_env)

    start = time.time()
    try:
        proc = await asyncio.create_subprocess_exec(
            *entry,
            query,
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            env=env,
            cwd=cwd,
        )
    except Exception as e:  # noqa: BLE001 — 启动失败需结构化回传
        return {"status": "error", "result": "", "error": f"启动失败: {e}", "duration": 0.0, "sid": sub_sid}

    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
    except TimeoutError:
        proc.terminate()
        try:
            await asyncio.wait_for(proc.wait(), timeout=3)
        except Exception:  # noqa: BLE001
            proc.kill()
            await proc.wait()
        return {
            "status": "error",
            "result": "",
            "error": f"超时（{timeout}s）",
            "duration": time.time() - start,
            "sid": sub_sid,
        }
    except (KeyboardInterrupt, asyncio.CancelledError):
        proc.kill()
        await proc.wait()
        raise

    out = stdout.decode("utf-8", errors="replace").strip()
    err = stderr.decode("utf-8", errors="replace").strip()
    duration = time.time() - start

    if out:
        return {"status": "ok", "result": out, "error": "", "duration": duration, "sid": sub_sid}
    return {
        "status": "error",
        "result": "",
        "error": err[-800:] if err else "worker 无输出",
        "duration": duration,
        "sid": sub_sid,
    }
