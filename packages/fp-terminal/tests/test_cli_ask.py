"""CLI ask 结构化契约 v2 — 问题块渲染后的输入解析

契约：docs/dev/ASYNC_AGENT_DESIGN.md §2（展示层负责解析，返回的已是最终文本）
- 空回车 → 采纳 suggest（无 suggest → 空 = 未回答）
- 编号（1..N）→ 选项原文
- 其他 → 自由文本原样
"""

import builtins

import pytest

from fp_cli.cli_io import CLIIO


def _ask(monkeypatch, typed: str, *, options=None, suggest="") -> str:
    monkeypatch.setattr(builtins, "input", lambda _p="": typed)
    io = CLIIO()
    import asyncio

    return asyncio.run(io.ask("执行吗？", options=options, suggest=suggest))


def test_empty_adopts_suggest(monkeypatch):
    assert _ask(monkeypatch, "", options=["y", "n"], suggest="n") == "n"


def test_empty_without_suggest_is_unanswered(monkeypatch):
    assert _ask(monkeypatch, "") == ""


def test_number_resolves_to_option_text(monkeypatch):
    assert _ask(monkeypatch, "2", options=["y", "n"], suggest="n") == "n"
    assert _ask(monkeypatch, "1", options=["y", "n"], suggest="n") == "y"


def test_out_of_range_number_is_raw_text(monkeypatch):
    # 编号越界 → 当作自由文本（不吞掉用户输入）
    assert _ask(monkeypatch, "9", options=["y", "n"]) == "9"


def test_free_text_passthrough(monkeypatch):
    assert _ask(monkeypatch, "自定义回答", options=["y", "n"]) == "自定义回答"


def test_eof_returns_empty(monkeypatch):
    def _raise(_p=""):
        raise EOFError

    monkeypatch.setattr(builtins, "input", _raise)
    import asyncio

    assert asyncio.run(CLIIO().ask("在吗？")) == ""


@pytest.mark.asyncio
async def test_no_options_still_works():
    import builtins as b

    orig = b.input
    b.input = lambda _p="": "hi"
    try:
        assert await CLIIO().ask("自由问答？") == "hi"
    finally:
        b.input = orig


# ── 显示时序：工具行不得挤占回答区（返工 n9） ──────────


@pytest.mark.asyncio
async def test_tool_line_flushed_before_question(monkeypatch):
    """tool_call 展示是异步调度的 — ask 必须先等它落盘再渲染问题块"""
    import asyncio

    from fp_cli.display import LLMStreamer

    order: list[str] = []

    async def slow_tool(self, name, args):
        await asyncio.sleep(0.05)  # 模拟逐段流式延迟
        order.append("tool_line")

    monkeypatch.setattr(LLMStreamer, "tool", slow_tool)
    monkeypatch.setattr(builtins, "input", lambda _p="": (order.append("input"), "y")[1])

    io = CLIIO()
    io.tool_call("ask_user", {"prompt": "执行吗？"})  # 入队展示任务
    assert await io.ask("执行吗？") == "y"
    assert order == ["tool_line", "input"], f"顺序错误: {order}"


@pytest.mark.asyncio
async def test_answer_area_isolated_from_during_ask_disp(monkeypatch):
    """问答挂起期间到达的 tool_result 必须在锁外排队，答完才打印"""
    import asyncio
    import threading

    from fp_cli.display import LLMStreamer

    order: list[str] = []
    gate = threading.Event()

    async def tracked_result(self, result):
        async with LLMStreamer._get_tool_lock():  # 模拟真实实现的锁行为
            order.append("result_line")

    def blocking_input(_p=""):
        order.append("input_enter")
        gate.wait(2.0)
        order.append("input_exit")
        return "y"

    monkeypatch.setattr(LLMStreamer, "tool_result_line", tracked_result)
    monkeypatch.setattr(builtins, "input", blocking_input)

    io = CLIIO()
    task = asyncio.create_task(io.ask("执行吗？"))
    await asyncio.sleep(0.1)  # ask 已进入 input（持锁中）
    assert order == ["input_enter"]

    io.tool_result("后台完成")  # 问答期间到达的展示
    await asyncio.sleep(0.1)
    assert "result_line" not in order, "回答区被插队！"

    gate.set()
    assert await task == "y"
    await asyncio.sleep(0.05)  # 给排队任务落盘时间
    assert order.index("input_exit") < order.index("result_line"), f"顺序错误: {order}"
