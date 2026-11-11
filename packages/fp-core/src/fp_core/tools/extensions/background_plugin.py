"""Background 插件 — 面向 LLM 的工具壳（ask_user + wait_job/kill_job/list_jobs）

契约：docs/dev/工具系统.md「background 插件」与「全工具后台化」。

分层（四层依赖模型 check_dep_layers）：本文件是 L2 扩展单元，只含工具声明与
执行壳；共享基础设施（jobs 服务、环顶注入队列、人类确认门、退出清算）
位于 L1：fp_core/core/jobs.py —— L2→L1 合法引用，L1 不反向 import L2。

不变量（见设计文档 §8）：
- I1 中心性：kill_job/退出清算独立于 LLM；io.ask 失败/无 io 降级，不挂死。
- I2 权威注入：用户话只以 user 角色到达（回答**裸文本**、human=True），tool
  result 只有收据 → 协议配对完好（repair_tool_ordering 0 错误）。
- I3 注入身份协议：非人类注入带 ⁂[kind] 标头（半角中括号），人类话语裸文本；
  shortcircuit 依此判连通块边界与幸存（引擎.md「注入消息身份协议」）。
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
from fp_core.core.jobs import (
    wait_for_pump as wait_for_pump,
)

# ── ask_user ────────────────────────────────────────────


async def _ask_user(params: dict[str, Any]) -> str:
    """向人类提问并等待回答（pull：恰在需要信息的时刻阻塞）。

    结构化契约 v2：options → 前端选择按钮；suggest → 推荐徽章（空输入采纳）；
    timeout → 秒级超时（默认 300，防前端断连后无限挂起）；
    ask_id → 收据/注入对账。回答以**裸文本**、``human=True`` 走环顶注入（I2；
    无标头 = 人类输入，见引擎.md「注入消息身份协议」）。

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
        # 人类话语：裸文本、human=True —— 不打 ⁂[ 标记、不加任何前缀（身份协议，
        # docs/dev/引擎.md「注入消息身份协议」）；ask_id 由收据 JSON 对账承载。
        inject_event("user_reply", reply, human=True)

    return json.dumps(
        {"status": "answered" if reply else "empty", "ask_id": ask_id, "reply_file": receipt},
        ensure_ascii=False,
    )


# ── wait / kill / list ──────────────────────────────────


async def _wait_job(params: dict[str, Any]) -> str:
    """形忙实等：等待后台任务终态，但可被泵消息礼让。

    契约见 tests/test_wait_yield.py：礼让零损失（终态由 _finalize 注入），
    故不引入任何新状态——只改"循环何时回到环顶"。
    """
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

    # ── 形忙实等（2026-11）───────────────────────────────
    # 仍是普通工具调用（外层仍处"忙"），只让**循环提前回到环顶**。
    # job 终态 与 泵消息 谁先到算谁：shield 保证"取消等待"不传播到 job.task。
    job_waiter = asyncio.ensure_future(asyncio.shield(job.task))
    pump_waiter = asyncio.ensure_future(wait_for_pump())
    done: set[asyncio.Future[Any]] = set()
    woken_by_pump = False
    try:
        done, _ = await asyncio.wait({job_waiter, pump_waiter}, timeout=timeout_f, return_when=asyncio.FIRST_COMPLETED)
        woken_by_pump = pump_waiter in done  # 精确归因：别把"job 侧完成"说成"被礼让"
    finally:
        # 取消**不吞**：把自己被取消如实传播给上层（agent 的取消即取消整轮）；
        # 而 job 因 shield 不受影响——"取消等待"≠"取消任务"。

        # 收回探测器：pump_waiter 每次必收；job_waiter 只取消 shield 壳（job.task 不受影响）
        pump_waiter.cancel()
        if not job_waiter.done():
            job_waiter.cancel()
        await asyncio.gather(pump_waiter, job_waiter, return_exceptions=True)

    # 失败以状态回报，不抛给 LLM（job 自身异常在 _finalize 里已落 status）
    if job.status == "done":
        return json.dumps(
            {"status": "done", "job_id": job_id, "result_file": job.result_path},
            ensure_ascii=False,
        )
    if job.status != "running":
        return json.dumps({"status": job.status, "job_id": job_id, "error": job.error}, ensure_ascii=False)

    # 此时 job 仍在运行：或超时，或被泵礼让。两者都**不取消、不丢结果**——
    # job 终态由 _finalize 经 inject_event 送进泵，与本次返回值无关。
    payload: dict[str, Any] = {
        "status": "yielded" if woken_by_pump else "still_running",
        "job_id": job_id,
        "elapsed": round(time.time() - job.started_at, 1),
    }
    if woken_by_pump:
        payload["note"] = "泵中有新消息，循环提前回顶；job 仍在运行，终态会另行注入，勿重复确认"
    return json.dumps(payload, ensure_ascii=False)


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
        inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）", wake=True)
    return json.dumps({"status": "killed", "job_id": job_id}, ensure_ascii=False)


_STATUS_ORDER = {"running": 0, "done": 1, "failed": 2, "killed": 3}


def _make_row(
    jid: Any,
    label: Any,
    status: Any,
    started: float,
    finished: float | None,
    result_path: Any,
    error: Any,
    now: float,
) -> dict[str, Any]:
    """构造一行任务事实：elapsed 有值、空 error 直接省略字段。"""
    row: dict[str, Any] = {
        "id": jid,
        "label": label,
        "status": status,
        "elapsed": round((finished or now) - started, 1),
        "result_path": result_path,
    }
    if error:
        row["error"] = error
    return row


async def _list_jobs(params: dict[str, Any]) -> str:
    """列出后台任务（框架渲染的事实表 —— 可见性独立于 LLM）。

    硬性筛选策略（无参数，直接筛）：
    1. 本进程任务全量显示 —— 含刚完成、结果尚未消费的终态任务；
    2. 磁盘遗留只保留仍在跑的活任务（并行实例）；**跨会话已结束的任务一律
       不显示**（完成时已环顶注入过；结果文件仍留在 JOB_DIR，可按 id 直读）；
    3. 排序：running → done → failed → killed，组内开始时间新者在前。
    """
    sweep_stale()
    now = time.time()
    entries: list[tuple[int, float, dict[str, Any]]] = []
    seen: set[str] = set()
    for job in job_registry.values():
        seen.add(job.id)
        row = _make_row(
            job.id,
            job.label,
            job.status,
            job.started_at,
            job.finished_at,
            job.result_path,
            job.error,
            now,
        )
        entries.append((_STATUS_ORDER.get(str(job.status), 9), -job.started_at, row))
    ensure_dir()
    for fname in sorted(os.listdir(JOB_DIR)):
        if not fname.endswith(".json"):
            continue
        data = load_file_job(fname[:-5])
        if not data or data.get("id") in seen:
            continue
        if data.get("status") != "running":
            continue  # 跨会话终态 = 历史垃圾，硬筛掉
        started = float(data.get("started_at") or 0.0)
        row = _make_row(
            data.get("id"),
            data.get("label"),
            data.get("status"),
            started,
            data.get("finished_at"),
            data.get("result_path"),
            data.get("error"),
            now,
        )
        entries.append((_STATUS_ORDER.get(str(data.get("status")), 9), -started, row))
    entries.sort(key=lambda e: (e[0], e[1]))
    rows = [e[2] for e in entries]
    return json.dumps({"jobs": rows, "count": len(rows)}, ensure_ascii=False)


# ── 插件注册（多工具模式） ──────────────────────────────

_ASK_DESC = (
    "向用户提问并等待其回答（pull 式人类交互）。回答以用户消息（裸文本、无标头）进入上下文，"
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
            "description": "等待某个后台任务完成（形忙实等：外层仍处忙，但泵里有新消息时会礼让）。"
            "精确在完成一刻返回（无轮询）。"
            "返回 yielded = 被泵中消息礼让、job 仍在跑（终态会另行注入，勿重复确认），可再次等待；"
            "返回 still_running = 超时未完成，可再次等待。"
            "等待期间请用 alarm 设定自查时间点，不要用 sleep 轮询。",
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
            "description": (
                "列出后台任务（框架事实表）：本会话全部任务 + 仍在运行的跨实例任务，"
                "按 running→done→failed→killed 排序。跨会话已结束的任务不显示"
                "（完成时已注入通知；结果文件仍可按 id 从任务目录直读）。"
            ),
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
