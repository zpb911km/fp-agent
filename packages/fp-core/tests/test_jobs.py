"""jobs 服务测试 — start_job/drain/落盘/僵尸清理/退出清算

契约：docs/dev/ASYNC_AGENT_DESIGN.md §3/§7
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
async def test_terminal_injects_are_wake_level():
    """job 终态事实 = wake 级（异步系列接空闲泵）：空闲实例自动起一轮报告，不必等下次输入"""
    # done
    job = bp.start_job("快任务", asyncio.sleep(0.01, result="ok"))
    await asyncio.wait_for(job.task, timeout=5)
    assert jobs_impl.has_pending_wake(), "job_done 必须是唤醒级"
    assert bp.drain_ready()[0]["kind"] == "job_done"
    assert not jobs_impl.has_pending_wake()

    # failed
    async def _boom():
        raise RuntimeError("刻意失败")

    job2 = bp.start_job("必败任务", _boom())
    await asyncio.wait_for(job2.task, timeout=5)
    assert jobs_impl.has_pending_wake(), "job_failed 必须是唤醒级"
    assert bp.drain_ready()[0]["kind"] == "job_failed"

    # killed（退出清算）
    bp.start_job("长任务", asyncio.sleep(60))
    await asyncio.sleep(0.05)
    bp.shutdown_all("清理")
    assert jobs_impl.has_pending_wake(), "job_killed（shutdown_all）必须是唤醒级"
    bp.drain_ready()
    await asyncio.sleep(0.1)  # 给 CancelledError 分支收尾机会
    assert bp.drain_ready() == [], "幂等守卫：迟到回调不得重复注入"


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


def _seed_disk(data: dict) -> None:
    with open(os.path.join(jobs_impl.JOB_DIR, f"{data['id']}.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)


@pytest.mark.asyncio
async def test_list_jobs_hard_filters_cross_session_terminal():
    """硬筛策略：跨会话终态文件不显示；活任务（活 pid）显示；死 pid running 清扫后隐藏"""
    now = time.time()
    # 1) 跨会话终态（垃圾主体）→ 必须隐藏
    _seed_disk({
        "id": "old-done",
        "label": "旧会话任务",
        "status": "done",
        "result_path": "/tmp/x.out.txt",
        "started_at": now - 9999,
        "finished_at": now - 9900,
        "error": "",
        "pid": os.getpid(),
    })
    _seed_disk({
        "id": "old-killed",
        "label": "旧会话被杀任务",
        "status": "killed",
        "result_path": "/tmp/y.out.txt",
        "started_at": now - 9999,
        "finished_at": now - 9900,
        "error": "x",
        "pid": os.getpid(),
    })
    # 2) 并行实例的活任务（pid 存活）→ 显示
    _seed_disk({
        "id": "alive-run",
        "label": "并行实例活任务",
        "status": "running",
        "result_path": "/tmp/z.out.txt",
        "started_at": now - 5,
        "finished_at": None,
        "error": "",
        "pid": os.getpid(),
    })
    # 3) 死 pid 的 running → sweep 清扫成 killed → 隐藏
    _seed_disk({
        "id": "dead-run",
        "label": "死进程任务",
        "status": "running",
        "result_path": "/tmp/w.out.txt",
        "started_at": now - 60,
        "finished_at": None,
        "error": "",
        "pid": 0,
    })

    job = bp.start_job("本会话任务", asyncio.sleep(60))
    res = json.loads(await bp._list_jobs({}))
    ids = [r["id"] for r in res["jobs"]]

    assert "old-done" not in ids and "old-killed" not in ids  # 跨会话终态 = 硬筛
    assert "dead-run" not in ids  # 清扫后成终态 → 隐藏
    assert "alive-run" in ids and job.id in ids
    # 排序：running 永远在最前
    assert res["jobs"][0]["status"] == "running"
    # 字段裁剪：空 error 不出现在行里；elapsed 必有值
    assert all(r.get("error", "x") for r in res["jobs"])
    assert all(isinstance(r["elapsed"], float) for r in res["jobs"])

    bp.shutdown_all("清理")
    await asyncio.sleep(0.05)
