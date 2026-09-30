"""Background 插件 — 面向 LLM 的工具壳（ask_user + wait_job/kill_job/list_jobs）

设计文档：仓库根 ASYNC_AGENT_DESIGN.md（唯一权威依据）。

分层（四层依赖模型 check_dep_layers）：本文件是 L2 扩展单元，只含工具声明与
执行壳；共享基础设施（jobs 服务、环顶注入队列、危险命令确认门、退出清算）
位于 L1：fp_core/core/jobs.py —— L2→L1 合法引用，L1 不反向 import L2。

不变量（见设计文档 §8）：
- I1 中心性：kill_job/退出清算独立于 LLM；io.ask 失败/无 io 降级，不挂死。
- I2 权威注入：用户话只以 user 角色到达（【用户回答】注入），tool result
  只有收据 → 协议配对完好（repair_tool_ordering 0 错误）。
- I3 shortcircuit 兼容：注入均为 user 角色消息，degenerate 不会删除人类话语。
"""

import asyncio
import contextlib
import json
import os
import time
import uuid
from typing import Any, cast

from fp_core.core.jobs import (  # noqa: F401 — re-export：下游/测试统一入口
    JOB_DIR,
    ask_lock,
    ensure_dir,
    inject_event,
    job_registry,
    load_file_job,
    persist_job,
    sweep_stale,
)
from fp_core.core.jobs import (
    drain_ready as drain_ready,
)
from fp_core.core.jobs import (
    pending_inject as pending_inject,
)
from fp_core.core.jobs import (
    shutdown_all as shutdown_all,
)
from fp_core.core.jobs import (
    start_job as start_job,
)

# ── ask_user ────────────────────────────────────────────


async def _ask_user(params: dict[str, Any]) -> str:
    """向人类提问并等待回答（pull：恰在需要信息的时刻阻塞）。

    结构化契约 v2：options → 前端选择按钮；suggest → 推荐徽章（空输入采纳）；
    timeout → 秒级超时（默认 300，防前端断连后无限挂起）；
    ask_id → 收据/注入对账。回答以【用户回答 …】user 角色走环顶注入（I2）。

    防滥问纪律（写给调用方 LLM）：只问会改变后续行为的阻塞级决策；
    必须给出推荐默认值（suggest）；话题级/探讨级问题应结束本轮，
    由用户自然发言，而非用本工具。主 agent 独占（worker 无 io）。
    """
    prompt = str(params.get("prompt") or "").strip()
    if not prompt:
        return json.dumps({"status": "error", "error": "prompt 必填"}, ensure_ascii=False)

    suggest = str(params.get("suggest") or "").strip()
    raw_options = params.get("options")
    options: list[str] = []
    if isinstance(raw_options, list):
        options = [str(o).strip() for o in cast("list[Any]", raw_options) if str(o).strip()]
    try:
        timeout = float(params.get("timeout") or 300.0)
    except (TypeError, ValueError):
        timeout = 300.0
    timeout = max(5.0, min(timeout, 3600.0))
    ask_id = f"ask_{uuid.uuid4().hex[:8]}"

    # 缺 io（headless/worker）→ 降级，不挂死（I1）
    try:
        from fp_core.core.agent import get_current_io

        io = get_current_io()
    except Exception:  # noqa: BLE001
        io = None
    if io is None:
        return json.dumps(
            {"status": "unavailable", "error": "no_io", "prompt": prompt},
            ensure_ascii=False,
        )

    if ask_lock.locked():
        # 已有一个 ask 在挂起等人 → 忙，交由 LLM 稍后重试
        return json.dumps({"status": "busy", "error": "已有 ask_user 在等待回答"}, ensure_ascii=False)

    async with ask_lock:
        try:
            reply = (
                await asyncio.wait_for(
                    io.ask(prompt, options=options, suggest=suggest, ask_id=ask_id),
                    timeout=timeout,
                )
            ).strip()
        except TimeoutError:
            return json.dumps(
                {"status": "timeout", "ask_id": ask_id, "timeout": timeout},
                ensure_ascii=False,
            )
        except Exception as e:  # noqa: BLE001 — 询问失败降级，不吞轮次
            return json.dumps({"status": "error", "error": str(e)}, ensure_ascii=False)

    # deferred（ACP 等无带内回复通道）：问题已展示给用户，回答将在
    # 用户下一条消息（新轮次）自然到达 — 不注入、不回执，如实告知 LLM。
    if not reply and getattr(io, "ask_deferred", False):
        return json.dumps(
            {
                "status": "deferred",
                "ask_id": ask_id,
                "note": "问题已展示给用户；请结束本轮等待，用户的下一条消息即为其回答",
            },
            ensure_ascii=False,
        )

    # 回执落盘 + 回答以 user 角色走环顶注入（权威注入原则 I2）
    ensure_dir()
    receipt = os.path.join(JOB_DIR, f"{ask_id}.json")
    with contextlib.suppress(OSError), open(receipt, "w", encoding="utf-8") as f:
        json.dump(
            {"ask_id": ask_id, "prompt": prompt, "reply": reply, "ts": time.time()},
            f,
            ensure_ascii=False,
        )

    if reply:
        # in_reply_to 随消息文本携带（消息只有 role/content，无 metadata 槽位）
        inject_event("user_reply", f"【用户回答 {ask_id}】{reply}")

    return json.dumps(
        {"status": "answered" if reply else "empty", "ask_id": ask_id, "reply_file": receipt},
        ensure_ascii=False,
    )


# ── wait / kill / list ──────────────────────────────────


async def _wait_job(params: dict[str, Any]) -> str:
    """阻塞等待后台任务完成（pull 正确形态：精确在完成一刻返回，不 sleep 轮询）。"""
    job_id = str(params.get("job_id") or "").strip()
    if not job_id:
        return json.dumps({"status": "error", "error": "job_id 必填"}, ensure_ascii=False)
    timeout = params.get("timeout")
    try:
        timeout_f = float(timeout) if timeout is not None else None
    except (TypeError, ValueError):
        timeout_f = None

    job = job_registry.get(job_id)
    if job is None:
        data = load_file_job(job_id)
        if data is None:
            return json.dumps({"status": "error", "error": f"job {job_id} 不存在"}, ensure_ascii=False)
        # 跨重启：只剩文件事实，无法等待 → 直接回报现状
        return json.dumps({"status": data.get("status", "unknown"), **data}, ensure_ascii=False)

    if job.task is None:
        return json.dumps({"status": job.status, "job_id": job_id}, ensure_ascii=False)

    try:
        if timeout_f is not None:
            await asyncio.wait_for(asyncio.shield(job.task), timeout=timeout_f)
        else:
            await job.task
    except TimeoutError:
        return json.dumps(
            {"status": "still_running", "job_id": job_id, "elapsed": round(time.time() - job.started_at, 1)},
            ensure_ascii=False,
        )
    except asyncio.CancelledError:
        # 自身被中断（Ctrl+C）：任务是否存活由 kill/清算决定，如实回报
        return json.dumps(
            {"status": "wait_interrupted", "job_id": job_id, "job_status": job.status},
            ensure_ascii=False,
        )
    except Exception as e:  # noqa: BLE001 — 任务失败以状态回报，不抛给 LLM
        return json.dumps({"status": "failed", "job_id": job_id, "error": str(e)}, ensure_ascii=False)

    if job.status == "done":
        return json.dumps(
            {"status": "done", "job_id": job_id, "result_file": job.result_path},
            ensure_ascii=False,
        )
    return json.dumps({"status": job.status, "job_id": job_id, "error": job.error}, ensure_ascii=False)


async def _kill_job(params: dict[str, Any]) -> str:
    """终止后台任务（进程组击杀由被调方 CancelledError 分支负责，如 bash killpg）。"""
    job_id = str(params.get("job_id") or "").strip()
    if not job_id:
        return json.dumps({"status": "error", "error": "job_id 必填"}, ensure_ascii=False)
    job = job_registry.get(job_id)
    if job is None:
        data = load_file_job(job_id)
        if data is None:
            return json.dumps({"status": "error", "error": f"job {job_id} 不存在"}, ensure_ascii=False)
        return json.dumps(
            {"status": "already_dead", "job_id": job_id, "job_status": data.get("status")},
            ensure_ascii=False,
        )

    if job.status != "running":
        return json.dumps({"status": "already_dead", "job_id": job_id, "job_status": job.status}, ensure_ascii=False)

    if job.task and not job.task.done():
        job.task.cancel()
        # 给 CancelledError 分支一次落盘机会（bash killpg 在其中执行）
        with contextlib.suppress(Exception):
            await asyncio.wait_for(asyncio.gather(job.task, return_exceptions=True), timeout=5)
    if job.status == "running":  # runner 未及更新（极端时序兜底）
        job.status = "killed"
        job.finished_at = time.time()
        persist_job(job)
        inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）")
    return json.dumps({"status": "killed", "job_id": job_id}, ensure_ascii=False)


async def _list_jobs(params: dict[str, Any]) -> str:
    """列出后台任务（框架渲染的事实表 —— 可见性独立于 LLM）。"""
    sweep_stale()
    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for job in job_registry.values():
        seen.add(job.id)
        rows.append({
            "id": job.id,
            "label": job.label,
            "status": job.status,
            "elapsed": round((job.finished_at or time.time()) - job.started_at, 1),
            "result_path": job.result_path,
            "error": job.error,
        })
    ensure_dir()
    for fname in sorted(os.listdir(JOB_DIR)):
        if not fname.endswith(".json"):
            continue
        data = load_file_job(fname[:-5])
        if data and data.get("id") not in seen:
            rows.append({
                "id": data.get("id"),
                "label": data.get("label"),
                "status": data.get("status"),
                "elapsed": None,
                "result_path": data.get("result_path"),
                "error": data.get("error"),
            })
    return json.dumps({"jobs": rows, "count": len(rows)}, ensure_ascii=False)


# ── 插件注册（多工具模式） ──────────────────────────────

_ASK_DESC = (
    "向用户提问并等待其回答（pull 式人类交互）。回答会以【用户回答】消息进入上下文，"
    "本工具返回收据。给出 options 时前端渲染为一键选择按钮（推荐）。"
    "防滥问纪律：只问会改变后续行为的阻塞级决策，必须通过 suggest 给出推荐默认值；"
    "话题级/探讨级问题应结束本轮让用户自然发言。仅主 agent 可用（子 agent 无 io 会返回 unavailable）。"
)

PLUGIN_DEFINITIONS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "ask_user",
            "description": _ASK_DESC,
            "parameters": {
                "type": "object",
                "properties": {
                    "prompt": {"type": "string", "description": "要问的问题（清晰、自包含）"},
                    "suggest": {"type": "string", "description": "推荐默认值（强烈建议提供）"},
                    "options": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": "可选项列表（2~4 个为宜）— 前端渲染为一键选择按钮",
                    },
                    "timeout": {
                        "type": "number",
                        "description": "最长等待秒数（默认 300，超时返回 status=timeout）",
                    },
                },
                "required": ["prompt"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "wait_job",
            "description": "阻塞等待某个后台任务完成并取回结果文件路径。"
            "精确在完成一刻返回（无轮询）。任务超时未完成会返回 still_running，可再次等待。",
            "parameters": {
                "type": "object",
                "properties": {
                    "job_id": {"type": "string", "description": "任务 id"},
                    "timeout": {"type": "number", "description": "最长等待秒数（不传=一直等）"},
                },
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "kill_job",
            "description": "终止一个后台任务（含其整个进程组）。用于中止失控/不再需要的任务。",
            "parameters": {
                "type": "object",
                "properties": {"job_id": {"type": "string", "description": "任务 id"}},
                "required": ["job_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_jobs",
            "description": "列出全部后台任务及其状态（框架事实表）。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
]

TOOL_MAP: dict[str, Any] = {
    "ask_user": _ask_user,
    "wait_job": _wait_job,
    "kill_job": _kill_job,
    "list_jobs": _list_jobs,
}


async def execute(params: dict[str, Any]) -> str:
    """兜底 executor（TOOL_MAP 命中时不会被调用）"""
    return json.dumps({"status": "error", "error": "unknown tool"}, ensure_ascii=False)
