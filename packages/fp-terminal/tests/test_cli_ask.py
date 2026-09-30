"""CLI ask 结构化契约 v2 — 问题块渲染后的输入解析

契约：ASYNC_AGENT_DESIGN.md §2（展示层负责解析，返回的已是最终文本）
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
