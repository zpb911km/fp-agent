"""jobs 服务测试 — start_job/drain/落盘/僵尸清理/退出清算

契约：ASYNC_AGENT_DESIGN.md §3/§7
- 完成回调只入队 + 落盘；drain 恰好一次（环顶串行注入的基础）
- 状态独立落盘 JOB_DIR（重启可查）
- shutdown_all：running → killed（I1：清算独立于 LLM）
- sweep_stale：死进程遗留 running 文件 → killed
"""

import asyncio
import json
import os
import time

import pytest

from fp_core.core import jobs as jobs_impl
from fp_core.tools.extensions import background_plugin as bp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """隔离 JOB_DIR 与 pending 队列"""
    bp.pending_inject.clear()
    monkeypatch.setattr(bp, "JOB_DIR", str(tmp_path))
    monkeypatch.setattr(jobs_impl, "JOB_DIR", str(tmp_path))  # IO 单点在 L1，双侧同步 patch
    bp.job_registry.clear()  # mutate 共享 dict（split 后插件/jobs 双侧同对象，勿替换绑定）
    yield
    bp.pending_inject.clear()


def _read_state(job_id: str) -> dict:
    with open(os.path.join(bp.JOB_DIR, f"{job_id}.json"), encoding="utf-8") as f:
        return json.load(f)


@pytest.mark.asyncio
async def test_job_done_persists_and_injects_once():
    """完成 → 落盘 done + 结果写 result_file + drain 恰好一次"""
    job = bp.start_job("echo hi", asyncio.sleep(0.01, result="输出内容\n第二行"))

    res = await asyncio.wait_for(job.task, timeout=5)
    assert res is None  # _runner 吞掉返回值（已落盘）

    data = _read_state(job.id)
    assert data["status"] == "done"
    assert data["label"] == "echo hi"
    # 结果文件内容 = 协程返回值
    with open(job.result_path, encoding="utf-8") as f:
        assert f.read() == "输出内容\n第二行"

    drained = bp.drain_ready()
    assert len(drained) == 1
    assert drained[0]["kind"] == "job_done"
    assert "【系统事实】" in drained[0]["content"]
    assert job.id in drained[0]["content"]
    # 恰好一次
    assert bp.drain_ready() == []


@pytest.mark.asyncio
async def test_job_failure_recorded():
    async def _boom():
        raise RuntimeError("刻意失败")

    job = bp.start_job("必败任务", _boom())
    await asyncio.wait_for(job.task, timeout=5)

    data = _read_state(job.id)
    assert data["status"] == "failed"
    assert "刻意失败" in data["error"]

    drained = bp.drain_ready()
    assert len(drained) == 1
    assert drained[0]["kind"] == "job_failed"


@pytest.mark.asyncio
async def test_kill_job_marks_killed():
    job = bp.start_job("长任务", asyncio.sleep(60))
    await asyncio.sleep(0.05)

    res = json.loads(await bp._kill_job({"job_id": job.id}))
    assert res["status"] == "killed"

    # runner 的 CancelledError 分支已落盘
    await asyncio.sleep(0.05)
    data = _read_state(job.id)
    assert data["status"] == "killed"

    drained = bp.drain_ready()
    assert any(d["kind"] == "job_killed" for d in drained)


@pytest.mark.asyncio
async def test_shutdown_all_kills_running():
    """退出清算（I1）：running → killed 且 cancel"""
    job = bp.start_job("后台跑", asyncio.sleep(60))
    await asyncio.sleep(0.05)
    assert _read_state(job.id)["status"] == "running"

    bp.shutdown_all("测试退出")
    await asyncio.sleep(0.1)  # 给 CancelledError 分支落盘机会

    data = _read_state(job.id)
    # shutdown_all 同步置 killed；runner 分支也置 killed — 取决于时序
    assert data["status"] in ("killed",)
    assert job.task is not None and job.task.cancelled()


def test_sweep_stale_marks_dead_pid():
    """死进程遗留的 running 文件 → killed（僵尸清理）"""
    bp.ensure_dir()
    stale_id = "deadbeef01"
    with open(os.path.join(bp.JOB_DIR, f"{stale_id}.json"), "w", encoding="utf-8") as f:
        json.dump(
            {
                "id": stale_id,
                "label": "上个进程的遗孤",
                "status": "running",
                "pid": 2**22 + 12345,  # 极大概率不存在的 pid
                "started_at": time.time(),
            },
            f,
        )

    bp.sweep_stale()

    data = _read_state(stale_id)
    assert data["status"] == "killed"
    assert "stale" in data["error"]


@pytest.mark.asyncio
async def test_list_jobs_merges_memory_and_files():
    job = bp.start_job("活任务", asyncio.sleep(60))
    res = json.loads(await bp._list_jobs({}))
    assert res["count"] >= 1
    ids = [r["id"] for r in res["jobs"]]
    assert job.id in ids
    bp.shutdown_all("清理")
    await asyncio.sleep(0.05)
