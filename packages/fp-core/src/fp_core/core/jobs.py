"""后台任务基础设施 — pull 式主动 agent 的内核侧实现（L1）。

设计文档：仓库根 ASYNC_AGENT_DESIGN.md（唯一权威依据）。

分层说明：本模块是 L1 内核（四层依赖模型禁止 L1 静态 import L2 扩展），
因此 jobs 服务、环顶注入队列、危险命令确认门、退出清算等**共享基础设施**
放在这里；面向 LLM 的工具壳（ask_user/wait_job/kill_job/list_jobs）留在
tools/extensions/background_plugin.py（L2），经 L2→L1 合法引用本模块。

三件套：
1. jobs 服务  — start_job 把任意 awaitable 移交后台，完成事实入 pending 队列，
   agent 环顶 drain_ready() 串行注入（【系统事实】标记），无锁竞态。
   状态独立落盘 JOB_DIR（不混进 session context），重启可查、可清算。
2. 注入队列   — 完成回调/ask 回答只入队；环顶 drain 前不进对话。
3. 确认门     — human_confirm：危险命令命中 BLOCK 规则时向人求批准
   （ask_user 的杀手应用），无 io/非交互通道降级 None，不挂死。

不变量（见设计文档 §8）：
- I1 中心性：shutdown_all/确认门独立于 LLM；io.ask 失败/无 io 降级。
- I2 权威注入：用户话只以 user 角色到达；系统事实带【系统事实】前缀；
  tool result 只有收据 → 协议配对完好（repair_tool_ordering 0 错误）。
- I3 shortcircuit 兼容：注入均为 user 角色消息，degenerate 不会删除。
"""

import asyncio
import contextlib
import json
import os
import tempfile
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any, cast

# ── 存储布局 ────────────────────────────────────────────
JOB_DIR = os.path.join(tempfile.gettempdir(), "fp_jobs")

# 环顶待注入队列（完成回调/ask 回答只入队；agent 环顶串行 drain —— 无锁竞态）
pending_inject: deque[dict[str, str]] = deque()

# ask 单 flight 锁（防止并行工具同时挂起等人）
ask_lock = asyncio.Lock()


def ensure_dir() -> None:
    with contextlib.suppress(OSError):
        os.makedirs(JOB_DIR, exist_ok=True)


def _job_file(job_id: str) -> str:
    return os.path.join(JOB_DIR, f"{job_id}.json")


def _result_file(job_id: str) -> str:
    return os.path.join(JOB_DIR, f"{job_id}.out.txt")


# ── Job 数据 ────────────────────────────────────────────


@dataclass
class Job:
    """内存态 job 句柄；落盘副本见 JOB_DIR/{id}.json（重启后仅文件可见）"""

    id: str
    label: str
    status: str = "running"  # running | done | failed | killed
    task: "asyncio.Task[None] | None" = None
    result_path: str = ""
    started_at: float = field(default_factory=time.time)
    finished_at: float | None = None
    error: str = ""


job_registry: dict[str, Job] = {}


def persist_job(job: Job) -> None:
    ensure_dir()
    with contextlib.suppress(OSError), open(_job_file(job.id), "w", encoding="utf-8") as f:
        json.dump(
            {
                "id": job.id,
                "label": job.label,
                "status": job.status,
                "result_path": job.result_path,
                "started_at": job.started_at,
                "finished_at": job.finished_at,
                "error": job.error,
                "pid": os.getpid(),
            },
            f,
            ensure_ascii=False,
        )


def inject_event(kind: str, content: str) -> None:
    """把一条待注入消息入队（环顶 drain 前不进对话 —— 串行化保证无竞态）"""
    pending_inject.append({"kind": kind, "content": content})


def drain_ready() -> list[dict[str, str]]:
    """agent 环顶调用：取走全部待注入消息（每条恰好一次）。

    Returns:
        [{"kind": "job_done"|"job_killed"|"job_failed"|"user_reply", "content": str}, ...]
    """
    out = list(pending_inject)
    pending_inject.clear()
    return out


# ── jobs 服务 ───────────────────────────────────────────


async def _runner(job: Job, coro: Any) -> None:
    try:
        res = await coro
        # 协程返回 str → 落盘到结果文件（wait_job/注入消息引用该路径）
        if isinstance(res, str) and job.result_path:
            ensure_dir()
            with contextlib.suppress(OSError), open(job.result_path, "w", encoding="utf-8") as f:
                f.write(res)
    except asyncio.CancelledError:
        job.status = "killed"
        job.finished_at = time.time()
        persist_job(job)
        inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）")
        raise
    except Exception as e:  # noqa: BLE001 — 任务失败是数据而非崩溃
        job.status = "failed"
        job.error = str(e)
        job.finished_at = time.time()
        persist_job(job)
        inject_event("job_failed", f"【系统事实】后台任务失败：{job.label}（{job.id}），error={e}")
    else:
        job.status = "done"
        job.finished_at = time.time()
        persist_job(job)
        inject_event(
            "job_done",
            f"【系统事实】后台任务已完成：{job.label}（{job.id}），状态=done，结果见 {job.result_path}",
        )


def start_job(label: str, coro: Any, job_id: str | None = None) -> Job:
    """把一个 awaitable 移交后台执行（不阻塞调用方）。

    Args:
        label: 人类可读标签（注入消息与 /jobs 列表用）
        coro: 任意 awaitable（如 _execute_bash(...) 协程）
        job_id: 可选指定 id（测试用）

    Returns:
        Job 句柄（id / result_path 已定）
    """
    ensure_dir()
    jid = job_id or uuid.uuid4().hex[:8]
    job = Job(id=jid, label=label, result_path=_result_file(jid))
    job_registry[jid] = job
    persist_job(job)
    job.task = asyncio.get_running_loop().create_task(_runner(job, coro))
    return job


def sweep_stale() -> None:
    """启动清扫：上个进程遗留的 running 文件 → 标 killed（僵尸清理）。

    仅清扫 pid 已死的文件，避免误杀并行 FP 实例的活 job。
    """
    ensure_dir()
    for fname in os.listdir(JOB_DIR):
        if not fname.endswith(".json"):
            continue
        with contextlib.suppress(Exception):
            with open(os.path.join(JOB_DIR, fname), encoding="utf-8") as f:
                data = json.load(f)
            if data.get("status") != "running":
                continue
            if data.get("id") in job_registry:
                continue
            pid = int(data.get("pid") or 0)
            alive = False
            if pid > 0:
                with contextlib.suppress(OSError, ProcessLookupError):
                    os.kill(pid, 0)
                    alive = True
            if not alive:
                data["status"] = "killed"
                data["error"] = data.get("error") or "进程退出时未完成（stale 清扫）"
                with (
                    contextlib.suppress(OSError),
                    open(os.path.join(JOB_DIR, fname), "w", encoding="utf-8") as f,
                ):
                    json.dump(data, f, ensure_ascii=False)


sweep_stale()


def shutdown_all(reason: str = "会话退出") -> None:
    """退出清算：未完成任务 → cancel（bash 会在 CancelledError 中 killpg）
    并落盘 killed。由 agent.shutdown 调用（I1：清算独立于 LLM）。
    """
    for job in list(job_registry.values()):
        if job.status == "running" and job.task and not job.task.done():
            job.task.cancel()
        if job.status == "running":
            job.status = "killed"
            job.error = job.error or f"{reason}时未完成"
            job.finished_at = time.time()
            persist_job(job)
            inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）")


def load_file_job(job_id: str) -> dict[str, Any] | None:
    try:
        with open(_job_file(job_id), encoding="utf-8") as f:
            return cast("dict[str, Any]", json.load(f))
    except (OSError, ValueError):
        return None


# ── 危险命令确认门（ask_user 的杀手应用 — 设计 §4） ─────


async def human_confirm(prompt: str, suggest: str = "n") -> bool | None:
    """向人求得批准（True=同意 False=拒绝 None=无法询问）。

    无 io / ask 失败 / 非交互通道问不到人 → None，调用方回落原语义
    （如 bash 的拦截消息）—— headless 行为不回归。
    """
    try:
        from fp_core.core.agent import get_current_io

        io = get_current_io()
    except Exception:  # noqa: BLE001
        return None
    if io is None:
        return None
    if ask_lock.locked():
        return None  # 已有交互挂起 → 不叠加询问，回落默认
    async with ask_lock:
        try:
            reply = (await io.ask(f"{prompt}\n[推荐默认值: {suggest}]")).strip()
        except Exception:  # noqa: BLE001
            return None
    if not reply:
        return None
    return reply.lower() in ("y", "yes", "是", "确认", "ok")
