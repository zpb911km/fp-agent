"""会话工作目录（contextvar）— 多会话并存的正确性地基。

为什么必须有这层测试：改造前 cwd 靠 `os.chdir` 承载，那是**进程级**全局可变
状态；正确性完全依赖「同一时刻只有一个会话在跑」这条纪律，没有任何语言机制
保障。改成 ContextVar 后正确性来自「按 Task 隔离」，而这正是**唯一不能靠
肉眼 review 保证**的性质 —— 它只在「两个 Task 交替 await」时才暴露。

因此这里断言的不是「值能存能取」，而是三条**变异敏感**的性质：
1. 设置 cwd 不产生进程副作用（`os.getcwd()` 不变）——否则重回 chdir 老路；
2. 相对路径按会话 cwd 解析，绝对路径原样保留（不 realpath，避免回显漂移）；
3. **两个并发 Task 各看各的 cwd** —— 变异掉 ContextVar 改用模块级变量/全局
   字典，此用例必红；改回 os.chdir 则用例 1 必红。
"""

from __future__ import annotations

import asyncio
import os

import pytest

from fp_core.core.cwd import (
    current_cwd,
    cwd_scope,
    reset_cwd,
    resolve_path,
    set_cwd,
)


def test_falls_back_to_process_cwd_when_unset():
    """未设置 = 进程当前目录：终端/单会话前端不设它，语义必须与改造前一致。"""
    assert current_cwd() == os.getcwd()


def test_set_cwd_has_no_process_side_effect(tmp_path):
    """MUST：设置会话 cwd 不得改变进程 cwd（旧实现是 os.chdir）。"""
    origin = os.getcwd()
    token = set_cwd(str(tmp_path))
    try:
        assert current_cwd() == str(tmp_path)
        assert os.getcwd() == origin, "不得 chdir：进程 cwd 是全局状态，会串掉别的会话"
    finally:
        reset_cwd(token)
    assert current_cwd() == origin


def test_resolve_path_uses_session_cwd(tmp_path):
    with cwd_scope(str(tmp_path)):
        assert resolve_path("a/b.py") == os.path.join(str(tmp_path), "a/b.py")
        # 绝对路径原样返回：不 realpath，保留用户写法（软链不被解析成实体）
        assert resolve_path("/etc/host") == "/etc/host"
        # 空串原样透传（避免 join 出会话根目录冒充文件路径）
        assert resolve_path("") == ""


def test_cwd_scope_restores_on_exception(tmp_path):
    origin = os.getcwd()
    with pytest.raises(RuntimeError), cwd_scope(str(tmp_path)):
        assert current_cwd() == str(tmp_path)
        raise RuntimeError("boom")
    assert current_cwd() == origin, "异常路径也必须复位，否则污染的 Task 会带偏后续解析"


@pytest.mark.asyncio
async def test_concurrent_tasks_are_isolated(tmp_path):
    """核心性质：ContextVar 按 Task 隔离 —— 两个会话并发不会互相改写。

    这是「将来放开并发」的唯一前提。若实现退回模块级变量，此用例必红。
    """
    dir_a = tmp_path / "a"
    dir_b = tmp_path / "b"
    dir_a.mkdir()
    dir_b.mkdir()
    seen: dict[str, str] = {}
    both_set = asyncio.Event()
    counter = {"n": 0}

    async def worker(name: str, path, other_set: asyncio.Event):
        set_cwd(str(path))
        counter["n"] += 1
        if counter["n"] == 2:
            both_set.set()
        # 等对方也设置完，再读自己看到的值：此刻两个 Task 的 cwd 必然不同
        await asyncio.wait_for(both_set.wait(), timeout=5)
        await asyncio.sleep(0)  # 让出控制权，制造交替
        seen[name] = current_cwd()

    await asyncio.gather(
        worker("a", dir_a, both_set),
        worker("b", dir_b, both_set),
    )

    assert seen["a"] == str(dir_a), "A 的 cwd 被 B 覆盖 → ContextVar 没有按 Task 隔离"
    assert seen["b"] == str(dir_b)


@pytest.mark.asyncio
async def test_child_task_inherits_parent_cwd(tmp_path):
    """继承（而不是隔离到空）：turn 内 spawn 的子 Task 必须看到所属会话的 cwd。"""
    with cwd_scope(str(tmp_path)):
        seen = await asyncio.create_task(_read_cwd())
    assert seen == str(tmp_path)


async def _read_cwd() -> str:
    await asyncio.sleep(0)
    return current_cwd()
