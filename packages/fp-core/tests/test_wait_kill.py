"""wait_job/kill_job 测试 — pull 精确等待（不 sleep 轮询）

契约：docs/dev/ASYNC_AGENT_DESIGN.md §5/§7
- 完成 → done + result_file
- 超时 → still_running + elapsed（任务继续活着）
- kill 后 wait → killed/already_dead
- 不存在 → error
- 跨重启（只剩文件）→ 直接回报文件事实
"""

import asyncio
import json

import pytest

from fp_core.core import jobs as jobs_impl
from fp_core.tools.extensions import background_plugin as bp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    bp.pending_inject.clear()
    monkeypatch.setattr(bp, "JOB_DIR", str(tmp_path))
    monkeypatch.setattr(jobs_impl, "JOB_DIR", str(tmp_path))  # IO 单点在 L1，双侧同步 patch
    bp.job_registry.clear()  # mutate 共享 dict（split 后插件/jobs 双侧同对象，勿替换绑定）
    yield
    bp.pending_inject.clear()


@pytest.mark.asyncio
async def test_wait_completes_with_result_file():
    job = bp.start_job("短任务", asyncio.sleep(0.05, result="final-result"))
    res = json.loads(await bp._wait_job({"job_id": job.id}))
    assert res["status"] == "done"
    assert res["result_file"] == job.result_path
    with open(job.result_path, encoding="utf-8") as f:
        assert f.read() == "final-result"


@pytest.mark.asyncio
async def test_wait_timeout_keeps_running():
    job = bp.start_job("长任务", asyncio.sleep(60))
    res = json.loads(await bp._wait_job({"job_id": job.id, "timeout": 0.1}))
    assert res["status"] == "still_running"
    assert res["elapsed"] >= 0.1
    # 任务没被超时干掉
    assert _status(job.id) in ("running",)
    bp.shutdown_all("清理")
    await asyncio.sleep(0.05)


@pytest.mark.asyncio
async def test_wait_after_kill():
    job = bp.start_job("被杀任务", asyncio.sleep(60))
    await asyncio.sleep(0.05)
    await bp._kill_job({"job_id": job.id})
    res = json.loads(await bp._wait_job({"job_id": job.id}))
    assert res["status"] in ("killed", "wait_interrupted")


@pytest.mark.asyncio
async def test_kill_unknown_job():
    res = json.loads(await bp._kill_job({"job_id": "nonexistent"}))
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_wait_unknown_job():
    res = json.loads(await bp._wait_job({"job_id": "nonexistent"}))
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_wait_missing_job_id():
    res = json.loads(await bp._wait_job({}))
    assert res["status"] == "error"


@pytest.mark.asyncio
async def test_wait_file_only_job_reports_from_disk():
    """跨重启：内存无 job 但文件存在 → 直接回报文件事实（不假装能等）"""
    import os
    import time

    bp.ensure_dir()
    jid = "fileonly1"
    with open(os.path.join(bp.JOB_DIR, f"{jid}.json"), "w", encoding="utf-8") as f:
        json.dump(
            {"id": jid, "label": "旧进程的任务", "status": "done", "started_at": time.time()},
            f,
        )
    res = json.loads(await bp._wait_job({"job_id": jid}))
    assert res["status"] == "done"


def _status(job_id: str) -> str:
    return bp.job_registry[job_id].status
