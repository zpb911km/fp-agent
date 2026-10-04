"""后台任务基础设施 — pull 式主动 agent 的内核侧实现（L1）。

设计文档：docs/dev/ASYNC_AGENT_DESIGN.md（唯一权威依据）。

分层说明：本模块是 L1 内核（四层依赖模型禁止 L1 静态 import L2 扩展），
因此 jobs 服务、环顶注入队列、危险命令确认门、退出清算等**共享基础设施**
放在这里；面向 LLM 的工具壳（ask_user/wait_job/kill_job/list_jobs）留在
tools/extensions/background_plugin.py（L2），经 L2→L1 合法引用本模块。

三件套：
1. jobs 服务  — start_job 把任意 awaitable 移交后台，完成事实入 pending 队列，
   agent 环顶 drain_ready() 串行注入（非人类项带 ⁂[kind] 身份标头，见下），
   无锁竞态。状态独立落盘 JOB_DIR（不混进 session context），重启可查、可清算。
2. 注入队列   — 完成回调/ask 回答只入队；环顶 drain 前不进对话。
3. 确认门     — human_confirm：危险命令命中 BLOCK 规则时向人求批准
   （ask_user 的杀手应用），无 io/非交互通道降级 None，不挂死。

不变量（见设计文档 §8）：
- I1 中心性：shutdown_all/确认门独立于 LLM；io.ask 失败/无 io 降级。
- I2 权威注入：用户话只以 user 角色到达（裸文本）；系统事实正文带【系统事实】
  前缀、身份带 ⁂[kind] 标头；tool result 只有收据 → 协议配对完好。
- I3 注入消息身份协议（docs/dev/引擎.md「注入消息身份协议」定死）：
  非人类注入内容以 ``⁂[<kind>]`` 开头（**半角中括号**硬规定），人类话语
  裸文本不打标；shortcircuit 依此判连通块边界与幸存 —— 人类话语恒保留，
  已闭合块内的标记消息可作噪声剪除。
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
@dataclass
class InjectItem:
    """一条待注入消息。

    ``wake=True`` 表示这是一条"唤醒级"事件：若实例当前空闲，应主动起一轮
    消费它（而非等下次用户输入）——见 ``Agent.process_wakeup`` / ``portal.run.wake``。
    忙碌时只入队，等当前轮结束后的空闲泵取走（人类优先，不打断）。
    """

    kind: str
    content: str
    wake: bool = False


pending_inject: deque[InjectItem] = deque()

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


# ── 注入消息身份协议（docs/dev/引擎.md「注入消息身份协议」定死） ──
# 非人类注入的内容以 `⁂[<kind>] ` 开头 —— **中括号硬性规定半角 `[` `]`**，
# kind 约束 [a-z][a-z0-9_]*；人类话语（human=True）保持裸文本不打标。
# shortcircuit 只认这一个符号判边界/幸存，永不枚举 kind（加新 kind 零改动）。
INJECT_MARK = "⁂["


def is_injected(content: str | None) -> bool:
    """内容是否为非人类注入消息（以 ``⁂[`` 开头 —— 身份协议标头）。"""
    return bool(content) and content.startswith(INJECT_MARK)


def inject_event(kind: str, content: str, *, wake: bool = False, human: bool = False) -> None:
    """把一条待注入消息入队（环顶 drain 前不进对话 —— 串行化保证无竞态）。

    Args:
        wake: True = 唤醒级事件。若实例空闲，应主动起一轮消费它
              （见 ``Agent.process_wakeup`` / ``portal.run.wake``）；
              忙碌则只入队，等当前轮结束后的空闲泵取走（人类优先，不打断）。
              wake 是**调度语义**，与消息身份无关。
        human: True = 人类话语（如 ask_user 回答）→ 内容保持**裸文本**、不打标，
               在 shortcircuit 中作为连通块边界（真正的用户轮次）。
               False（默认）= 系统/事件消息 → 内容打标 ``⁂[<kind>] <原文>``
               （半角中括号），shortcircuit 不把它当人类输入。
    """
    if not human:
        content = f"{INJECT_MARK}{kind}] {content}"
    pending_inject.append(InjectItem(kind, content, wake))


def has_pending_wake() -> bool:
    """是否有未消费的唤醒级事件（供空闲泵判断是否该起一轮）。"""
    return any(item.wake for item in pending_inject)


def drain_ready() -> list[dict[str, str]]:
    """agent 环顶调用：取走全部待注入消息（每条恰好一次）。

    Returns:
        [{"kind": "job_done"|"job_killed"|"job_failed"|"user_reply"|…, "content": str}, ...]
        非人类项 content 已带 ``⁂[kind]`` 标头；人类项（human=True）为裸文本。
    """
    out = [{"kind": item.kind, "content": item.content} for item in pending_inject]
    pending_inject.clear()
    return out


# ── jobs 服务 ───────────────────────────────────────────


def _finalize(job: Job, status: str, error: str = "", result: Any = None) -> None:
    """job 终态登记：结果落盘 + persist + 环顶注入（幂等 — 已终态则跳过）。

    注入为 **wake 级**：实例空闲时由空闲泵自动起一轮报告，不必等下一次用户输入
    （忙碌则保留到轮末，人类优先）。非终态注入（如 user_reply）仍为普通级。

    幂等守卫同时修复退出清算的双注入：shutdown_all 先标 killed 再 cancel，
    迟到的完成回调看到非 running 状态即让位，不会重复注入。
    """
    if job.status != "running":
        return
    job.status = status
    if error:
        job.error = error
    job.finished_at = time.time()
    if result is not None and job.result_path:
        text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False, default=str)
        ensure_dir()
        with contextlib.suppress(OSError), open(job.result_path, "w", encoding="utf-8") as f:
            f.write(text)
    persist_job(job)
    # 终态事实均为 wake 级：实例空闲时不必等下一次用户输入，空闲泵自动起一轮报告
    if status == "done":
        inject_event(
            "job_done",
            f"【系统事实】后台任务已完成：{job.label}（{job.id}），状态=done，结果见 {job.result_path}",
            wake=True,
        )
    elif status == "failed":
        inject_event(
            "job_failed",
            f"【系统事实】后台任务失败：{job.label}（{job.id}），error={job.error}",
            wake=True,
        )
    else:  # killed
        inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）", wake=True)


async def _runner(job: Job, coro: Any) -> None:
    try:
        res = await coro
    except asyncio.CancelledError:
        _finalize(job, "killed")
        raise
    except Exception as e:  # noqa: BLE001 — 任务失败是数据而非崩溃
        _finalize(job, "failed", error=str(e))
    else:
        _finalize(job, "done", result=res)


def _on_task_done(job: Job, task: "asyncio.Task[Any]") -> None:
    """adopt_task 的完成回调（与 _runner 同语义的终态登记）"""
    if task.cancelled():
        _finalize(job, "killed")
        return
    exc = task.exception()
    if exc is not None:
        _finalize(job, "failed", error=str(exc))
        return
    _finalize(job, "done", result=task.result())


def adopt_task(label: str, task: "asyncio.Task[Any]", job_id: str | None = None) -> Job:
    """把一个**已在运行**的 task 登记为后台 job（框架层超时移交用 — 语义同 start_job）。

    Args:
        label: 人类可读标签（注入消息与 /jobs 列表用）
        task: 已在运行的 asyncio.Task（调用方不得再自行 await 其结果）
        job_id: 可选指定 id（测试用）

    Returns:
        Job 句柄（id / result_path 已定）
    """
    ensure_dir()
    jid = job_id or uuid.uuid4().hex[:8]
    job = Job(id=jid, label=label, result_path=_result_file(jid))
    job_registry[jid] = job
    job.task = task
    persist_job(job)
    task.add_done_callback(lambda t: _on_task_done(job, t))
    return job


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

    def _close_unstarted_coro(_t: "asyncio.Task[Any]") -> None:
        # task 若在首步前被 cancel（如 start_job 后立刻 shutdown_all），
        # _runner 没机会 await 内层协程 → 兜底关闭，防 "never awaited" 警告
        if asyncio.iscoroutine(coro):
            coro.close()

    job.task.add_done_callback(_close_unstarted_coro)
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
            inject_event("job_killed", f"【系统事实】后台任务已终止：{job.label}（{job.id}）", wake=True)


def load_file_job(job_id: str) -> dict[str, Any] | None:
    try:
        with open(_job_file(job_id), encoding="utf-8") as f:
            return cast("dict[str, Any]", json.load(f))
    except (OSError, ValueError):
        return None


# ── 危险命令确认门（ask_user 的杀手应用 — 设计 §4） ─────


async def human_confirm(prompt: str, suggest: str = "n") -> bool | None:
    """向人求得批准（True=同意 False=拒绝 None=无法询问）。

    无 io / ask 失败 / 超时 / 非交互通道问不到人 → None，调用方回落原语义
    （如 bash 的拦截消息）—— headless 行为不回归。
    结构化契约 v2：options 渲染为 y/n 一键按钮，suggest=推荐徽章。
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
            reply = (
                await asyncio.wait_for(
                    io.ask(prompt, options=["y", "n"], suggest=suggest),
                    timeout=120.0,  # 确认门有界等待 — 超时回落原拦截消息
                )
            ).strip()
        except Exception:  # noqa: BLE001 — 含 TimeoutError/deferred 空答
            return None
    if not reply:
        return None  # deferred（ACP）/ 空答 → 回落原语义，不伪造表态
    return reply.lower() in ("y", "yes", "是", "确认", "ok")
