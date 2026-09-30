"""框架层全工具后台化测试 — background 参数 / FW_BG_TIMEOUT 自动移交 / 自管理豁免

契约（设计 §0 + tool_executor）：
- background=true → 立即 job 收据，完成落盘 + 环顶注入 job_done
- 前台超过 FW_BG_TIMEOUT → adopt_task 转后台，返回 backgrounded_on_timeout
- SELF_MANAGED 工具（bash/ask_user/wait_job/...）：schema 不注入、执行不拦截
- 用户中断：工具任务被取消且不登记悬挂 job
"""

import asyncio
import json

import pytest

from fp_core.core import jobs as jobs_impl
from fp_core.core import tool_executor as te_mod
from fp_core.core.tool_executor import ToolExecutor
from fp_core.tools import create_registry
from fp_core.tools.extensions import background_plugin as bp


@pytest.fixture(autouse=True)
def _clean_state(monkeypatch, tmp_path):
    """隔离 JOB_DIR 与 pending 队列（模式同 test_jobs：双侧 patch，dict 只清不换）"""
    bp.pending_inject.clear()
    monkeypatch.setattr(bp, "JOB_DIR", str(tmp_path))
    monkeypatch.setattr(jobs_impl, "JOB_DIR", str(tmp_path))
    bp.job_registry.clear()
    yield
    bp.pending_inject.clear()


def _make_executor(name: str, handler) -> ToolExecutor:
    registry = create_registry()
    registry.register_tool(
        name,
        {
            "type": "function",
            "function": {
                "name": name,
                "description": "测试工具",
                "parameters": {"type": "object", "properties": {}, "required": []},
            },
        },
        handler,
    )
    return ToolExecutor(registry)


def _call(name: str, arguments: str, call_id: str = "t1") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": name, "arguments": arguments},
    }


async def _await_status(job, status: str, timeout: float = 3.0) -> None:
    """等 job 落到终态（done_callback 经 call_soon 排队，轮询等它执行）"""
    for _ in range(int(timeout / 0.01)):
        if job.status == status:
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"job {job.id} 未在 {timeout}s 内进入 {status}（当前 {job.status}）")


@pytest.mark.asyncio
async def test_background_param_returns_receipt_and_completes():
    """background=true → 立即收据（非阻塞），完成后落盘 + 注入 job_done"""

    async def slow(params):
        await asyncio.sleep(0.05)
        return "SLOW-OK"

    ex = _make_executor("bg_slow", slow)
    out = await ex.execute(_call("bg_slow", '{"background": true}'))
    data = json.loads(out)
    assert data["status"] == "backgrounded"
    assert data["tool"] == "bg_slow"
    job = bp.job_registry[data["job_id"]]
    assert job.result_path == data["result_file"]

    await _await_status(job, "done")
    with open(job.result_path, encoding="utf-8") as f:
        assert f.read() == "SLOW-OK"
    events = bp.drain_ready()
    assert any(data["job_id"] in e["content"] and e["kind"] == "job_done" for e in events)


@pytest.mark.asyncio
async def test_timeout_auto_adopts_background(monkeypatch):
    """前台超时 → adopt_task 转后台，返回 backgrounded_on_timeout 收据（不断连）"""
    monkeypatch.setattr(te_mod, "FW_BG_TIMEOUT", 0.05)

    async def slow(params):
        await asyncio.sleep(0.25)
        return "LATE-OK"

    ex = _make_executor("bg_late", slow)
    out = await ex.execute(_call("bg_late", "{}"))
    data = json.loads(out)
    assert data["status"] == "backgrounded_on_timeout"
    assert data["job_id"]
    assert "0.05s" in data["note"] or "0.05" in data["note"]

    job = bp.job_registry[data["job_id"]]
    await _await_status(job, "done")
    with open(job.result_path, encoding="utf-8") as f:
        assert f.read() == "LATE-OK"


@pytest.mark.asyncio
async def test_fast_tool_blocks_normally_no_job():
    """前台未超时 → 原样返回结果，不登记任何 job"""

    async def fast(params):
        return "FAST"

    ex = _make_executor("bg_fast", fast)
    out = await ex.execute(_call("bg_fast", "{}"))
    assert out == "FAST"
    assert len(bp.job_registry) == 0
    assert not bp.pending_inject


def test_schema_injection_and_self_managed_exempt():
    """普通工具注入 background 参数；SELF_MANAGED 豁免；幂等"""
    ex = ToolExecutor(create_registry())
    defs = ex.get_definitions()
    by_name = {d["function"]["name"]: d for d in defs}

    # 普通工具：注入框架 background
    props = by_name["read_file"]["function"]["parameters"]["properties"]
    assert props["background"]["type"] == "boolean"
    assert "job_id" in props["background"]["description"]

    # 自管理豁免（bash 自带同名参数 → 跳过注入但自身存在，语义是它自己的）
    for name in ("ask_user", "wait_job", "background", "kill_job", "list_jobs", "reload"):
        if name in by_name:
            p = by_name[name]["function"]["parameters"]["properties"]
            assert "background" not in p, f"{name} 不应被注入 background"

    # 幂等：二次注入不覆盖、不报错
    ex.get_definitions()
    assert by_name["read_file"]["function"]["parameters"]["properties"]["background"] == props["background"]


@pytest.mark.asyncio
async def test_exception_propagates_like_before():
    """未超时路径的异常仍原样向上传播（ON_TOOL_ERROR 契约不变）"""

    async def boom(params):
        raise ValueError("boom")

    ex = _make_executor("bg_boom", boom)
    with pytest.raises(ValueError, match="boom"):
        await ex.execute(_call("bg_boom", "{}"))


@pytest.mark.asyncio
async def test_interrupt_cancels_tool_no_hanging_job():
    """中断（cancel）→ 工具任务被取消、不登记悬挂 job"""

    started = asyncio.Event()

    async def slow(params):
        started.set()
        await asyncio.sleep(10)
        return "never"

    ex = _make_executor("bg_int", slow)
    t = asyncio.create_task(ex.execute(_call("bg_int", "{}")))
    await started.wait()
    t.cancel()
    with pytest.raises(asyncio.CancelledError):
        await t
    await asyncio.sleep(0.05)
    assert len(bp.job_registry) == 0
