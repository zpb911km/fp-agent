"""wait_job「形忙实等」测试 —— 礼让泵消息而不丢失任务结果。

契约（docs/dev/引擎.md「环顶惰性注入与退出清算」+ 2026-11 形忙实等设计）：
- ``wait`` 仍是普通工具调用（外层仍处"忙"），仅**循环提前回到环顶**；
- 泵里有任何待注入消息 → wait 立即返回 ``yielded``，**不取消 job**；
- 结果不依赖 wait 返回值：job 终态由 ``_finalize`` inject_event 送进泵，
  故被礼让**零损失**；
- 若打断它的正是本 job 的终态 → 直接报终态（不得把"已完成"说成"被礼让"）；
- 无 timeout 语义不变（超时 → still_running；取消 → wait_interrupted）。

不变量：wait 不引入新状态（不新增 running/blocked 之类），只改"循环何时回顶"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from fp_core.core import jobs as jobs_impl
from fp_core.tools.extensions import background_plugin as bp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    bp.pending_inject.clear()
    monkeypatch.setattr(bp, "JOB_DIR", str(tmp_path))
    monkeypatch.setattr(jobs_impl, "JOB_DIR", str(tmp_path))
    bp.job_registry.clear()
    yield
    bp.pending_inject.clear()


def _wait(job_id: str, timeout: float | None = None) -> dict:
    params: dict = {"job_id": job_id}
    if timeout is not None:
        params["timeout"] = timeout
    return json.loads(asyncio.get_event_loop().run_until_complete(bp._wait_job(params)))  # type: ignore[arg-type]


# ── 泵信号本身 ────────────────────────────────────────


@pytest.mark.asyncio
async def test_wait_for_pump_wakes_on_inject_and_ignores_empty_signal():
    """信号只是提示：唤醒来自 inject_event；队列空时等待者不会被误唤醒。"""
    assert jobs_impl.has_pending() is False

    waiter = asyncio.ensure_future(jobs_impl.wait_for_pump())
    await asyncio.sleep(0.05)
    assert not waiter.done(), "队列为空时 wait_for_pump 不得返回"

    jobs_impl.inject_event("peer_ring", "hi", wake=True)
    await asyncio.wait_for(waiter, timeout=1.0)
    assert jobs_impl.has_pending() is True, "wait_for_pump 不得消费队列（消费权在环顶）"

    drained = jobs_impl.drain_ready()
    assert [d["kind"] for d in drained] == ["peer_ring"]
    assert jobs_impl.has_pending() is False


@pytest.mark.asyncio
async def test_wait_for_pump_shared_by_multiple_waiters():
    """多个礼让等待者共享同一次唤醒；判据是队列，故不会有人被漏掉。"""
    a = asyncio.ensure_future(jobs_impl.wait_for_pump())
    b = asyncio.ensure_future(jobs_impl.wait_for_pump())
    await asyncio.sleep(0.05)
    assert not a.done() and not b.done()

    jobs_impl.inject_event("alarm", "到点", wake=True)
    await asyncio.wait_for(asyncio.gather(a, b), timeout=1.0)
    assert jobs_impl.has_pending() is True


@pytest.mark.asyncio
async def test_wait_yielded_by_pump_message_without_killing_job():
    """泵来信 → wait 立刻 yielded；job 继续跑（礼让不是 cancel）。"""
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "slow-done"

    job = jobs_impl.start_job("slow", _slow())

    waiter = asyncio.ensure_future(bp._wait_job({"job_id": job.id}))
    await asyncio.sleep(0.05)
    assert waiter.done() is False, "无事时 wait 必须真的在等"

    jobs_impl.inject_event("peer_ring", "【铃声】邻居找你", wake=True)
    got = json.loads(await asyncio.wait_for(waiter, timeout=1.0))

    assert got["status"] == "yielded"
    assert got["job_id"] == job.id
    assert job.status == "running", "礼让不得取消 job"
    assert job.task is not None and not job.task.done()

    gate.set()
    await asyncio.wait_for(job.task, timeout=1.0)


@pytest.mark.asyncio
async def test_yielded_wait_loses_nothing_result_arrives_via_pump():
    """被礼让后 job 完成 → 终态照旧以 job_done 进泵（结果不依赖 wait 返回值）。"""
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "payload"

    job = jobs_impl.start_job("slow", _slow())
    waiter = asyncio.ensure_future(bp._wait_job({"job_id": job.id}))
    await asyncio.sleep(0.05)
    jobs_impl.inject_event("peer_ring", "铃声", wake=True)
    assert json.loads(await asyncio.wait_for(waiter, timeout=1.0))["status"] == "yielded"

    jobs_impl.drain_ready()  # 环顶消费掉铃声
    gate.set()
    await asyncio.wait_for(job.task, timeout=1.0)

    kinds = [i.kind for i in jobs_impl.pending_inject]
    assert "job_done" in kinds, "被礼让的 wait 不得吞掉终态事实"
    assert job.status == "done"


@pytest.mark.asyncio
async def test_wait_reports_done_not_yielded_when_job_already_finished():
    """打断它的正是本 job 的终态 → 报 done，不得说成 yielded（否则事实说两遍）。"""

    async def _quick() -> str:
        return "ok"

    job = jobs_impl.start_job("quick", _quick())
    await asyncio.wait_for(job.task, timeout=1.0)
    jobs_impl.inject_event("job_done", "【系统事实】完成了", wake=True)

    got = json.loads(await asyncio.wait_for(bp._wait_job({"job_id": job.id}), timeout=1.0))
    assert got["status"] == "done"


@pytest.mark.asyncio
async def test_wait_timeout_still_running_unchanged():
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "x"

    job = jobs_impl.start_job("slow", _slow())
    got = json.loads(await asyncio.wait_for(bp._wait_job({"job_id": job.id, "timeout": 0.05}), timeout=1.0))
    assert got["status"] == "still_running"
    assert job.status == "running"
    gate.set()
    await asyncio.wait_for(job.task, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_requires_job_id():
    """job_id 必填 —— 一旦允许无对象，wait 就退化成 sleep。"""
    got = json.loads(await bp._wait_job({}))
    assert got["status"] == "error"


@pytest.mark.asyncio
async def test_wait_interrupted_by_cancel_reports_interrupted():
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "x"

    job = jobs_impl.start_job("slow", _slow())
    waiter = asyncio.ensure_future(bp._wait_job({"job_id": job.id}))
    await asyncio.sleep(0.05)
    waiter.cancel()
    with pytest.raises(asyncio.CancelledError):
        await waiter
    assert job.status == "running", "取消等待不影响任务存活"
    gate.set()
    await asyncio.wait_for(job.task, timeout=1.0)


@pytest.mark.asyncio
async def test_wait_does_not_consume_pump_queue():
    """礼让只"提前返回"，不得自己 drain —— 消费权在环顶，否则消息丢失。"""
    gate = asyncio.Event()

    async def _slow() -> str:
        await gate.wait()
        return "x"

    job = jobs_impl.start_job("slow", _slow())
    waiter = asyncio.ensure_future(bp._wait_job({"job_id": job.id}))
    await asyncio.sleep(0.05)
    jobs_impl.inject_event("alarm", "到点了", wake=True)
    await asyncio.wait_for(waiter, timeout=1.0)

    assert jobs_impl.has_pending() is True, "wait 不得替环顶消费注入队列"
    assert [i.kind for i in jobs_impl.pending_inject] == ["alarm"]
    gate.set()
    await asyncio.wait_for(job.task, timeout=1.0)
