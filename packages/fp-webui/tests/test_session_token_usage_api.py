"""会话卡片 token 消耗展示（/api/sessions 与 /api/sessions/search 下发摘要）。

契约分两层：
  1. meta.token_usage（fp-core 每轮问答落盘写入的累计快照）→ 后端摘成
     ``{total, prompt, completion, calls, hit, miss, hit_rate, short, detail, rows}``，
     展示串在后端拼好；
  2. 前端 sessions.js 只把 ``rows``（成品行）逐行插值进二级菜单。

此处只验第 1 层（第 2 层是 DOM 渲染，见 e2e/前端断言）。取舍要点：
  - 无用量（老会话/未调用过 LLM）→ ``None``，前端不渲染该行，不造 "0 tokens" 假数据；
  - ``rows`` 顺序 = 总计 → 输入未命中 → 输入命中 → 输出 → 命中率；
  - 模型未上报缓存细分时，输入退化成「输入」单行、命中率给 ``—``（不填 0%）；
  - ``short`` 必须一行放得下（千折 k、百万折 M，去掉无意义 ``.0``）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from fp_webui import main


def _usage(*, prompt: int, completion: int, calls: int, hit: int = 0, miss: int = 0) -> dict:
    total: dict = {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "call_count": calls,
    }
    if hit or miss:
        total["cache_hit_tokens"] = hit
        total["cache_miss_tokens"] = miss
    return {
        "total": total,
        "per_model": {"deepseek-flash": dict(total)},
    }


def _patch_portal(monkeypatch, sessions_obj: SimpleNamespace) -> None:
    fake = SimpleNamespace(
        ctl=SimpleNamespace(sessions=sessions_obj),
        run=SimpleNamespace(status=SimpleNamespace(session_id="s_cur")),
    )
    monkeypatch.setattr(main, "portal", fake)

    async def _noop() -> None:
        return None

    monkeypatch.setattr(main, "ensure_agent", _noop)


# ── 纯函数：摘要与格式 ──────────────────────────────


@pytest.mark.parametrize(
    ("n", "want"),
    [
        (0, "0"),
        (999, "999"),
        (1000, "1k"),
        (1250, "1.2k"),  # 1.25 → 1.2（四舍六入的银行家舍入在此不敏感）
        (11200, "11.2k"),
        (105_000, "105k"),
        (999_949, "999.9k"),
        (999_999, "1M"),  # 不留 "1000k" 的怪值
        (1_000_000, "1M"),
        (1_234_567, "1.23M"),
        (12_000_000, "12M"),
    ],
)
def test_fmt_tokens_is_one_line(n: int, want: str) -> None:
    assert main._fmt_tokens(n) == want


def test_token_summary_none_for_missing_or_zero() -> None:
    """无 token_usage / 结构不全 / call_count=0 → None（前端据此不渲染该行）"""
    assert main._token_summary(None) is None
    assert main._token_summary({}) is None
    assert main._token_summary({"total": "bad"}) is None
    assert main._token_summary({"total": {"total_tokens": 100, "call_count": 0}}) is None
    assert main._token_summary({"total": {"total_tokens": 0, "call_count": 3}}) is None


def test_token_summary_fields_and_detail() -> None:
    """摘要含总量/明细；detail 带 ↑提示 ↓回复 / 调用次数 / 缓存命中率 / 模型名"""
    out = main._token_summary(_usage(prompt=9000, completion=2000, calls=7, hit=8000, miss=1000))
    assert out is not None
    assert out["total"] == 11000
    assert out["prompt"] == 9000
    assert out["completion"] == 2000
    assert out["calls"] == 7
    assert out["short"] == "11k"  # 卡片行内只放这个
    assert "↑9k" in out["detail"]
    assert "↓2k" in out["detail"]
    assert "7 次调用" in out["detail"]
    assert "89%" in out["detail"]  # 8000/9000
    assert "deepseek-flash" in out["detail"]


def test_token_summary_detail_without_cache() -> None:
    """无缓存字段时不拼缓存命中率（不显示 "-" 或 0%）"""
    out = main._token_summary(_usage(prompt=100, completion=20, calls=1))
    assert out is not None
    assert "缓存" not in out["detail"]


# ── 二级菜单成品行（rows）：用户要的四项明细 ──────────


def test_rows_show_miss_hit_output_and_rate_in_order() -> None:
    """输入未命中 / 输入命中 / 输出 / 命中率 四行齐全，且顺序固定"""
    out = main._token_summary(_usage(prompt=9000, completion=2000, calls=7, hit=8000, miss=1000))
    assert out is not None
    rows = out["rows"]
    assert [r["l"] for r in rows] == ["总计", "输入未命中", "输入命中", "输出", "命中率"]
    assert rows[0]["v"] == "11k"  # 总计
    assert rows[1]["v"] == "1k"  # 未命中 = miss，不是 prompt
    assert rows[2]["v"] == "8k"  # 命中 = hit
    assert rows[3]["v"] == "2k"  # 输出 = completion
    assert rows[4]["v"] == "89%"  # 8000 / 9000
    # 每行都带悬浮说明；图标列不留空
    assert all(r["k"] and r["tip"] for r in rows)


def test_rows_numeric_fields_match_totals() -> None:
    """hit/miss/hit_rate 同口径下发，供前端做二次计算时不重算格式化串"""
    out = main._token_summary(_usage(prompt=9000, completion=2000, calls=7, hit=8000, miss=1000))
    assert out is not None
    assert out["hit"] == 8000
    assert out["miss"] == 1000
    assert out["hit_rate"] == pytest.approx(8000 / 9000)


def test_rows_degrade_honestly_without_cache_data() -> None:
    """无缓存细分：输入退化成 prompt 单行，命中率给 —（不伪造 0%）"""
    out = main._token_summary(_usage(prompt=100, completion=20, calls=1))
    assert out is not None
    rows = out["rows"]
    assert [r["l"] for r in rows] == ["总计", "输入", "输出", "命中率"]
    assert rows[1]["v"] == "100"  # 全量输入
    assert rows[3]["v"] == "—"
    assert out["hit"] == 0 and out["miss"] == 0
    assert out["hit_rate"] is None  # 未知，不是 0.0
    assert "未上报" in rows[3]["tip"]


def test_rows_output_tip_mentions_reasoning_when_present() -> None:
    """思考 token 已含在 completion 内，只进「输出」的悬浮说明，不另开一行"""
    data = _usage(prompt=100, completion=200, calls=1, hit=80, miss=20)
    data["total"]["reasoning_tokens"] = 150
    out = main._token_summary(data)
    assert out is not None
    out_row = next(r for r in out["rows"] if r["l"] == "输出")
    assert out_row["v"] == "200"  # 不把 reasoning 再加一遍
    assert "150" in out_row["tip"]


# ── 端点契约 ────────────────────────────────────────


def test_list_sessions_exposes_token_usage(monkeypatch) -> None:
    """列表端点把 meta.token_usage 摘成 token_usage 字段（老会话 → None）"""
    info = {
        "s_old": {  # 老会话：meta 里没有 token_usage
            "message_count": 4,
            "created": "2026-11-01 10:00:00",
            "updated": "2026-11-01 10:00:00",
            "summary": "老会话",
        },
        "s_cur": {
            "message_count": 2,
            "created": "2026-11-02 10:00:00",
            "updated": "2026-11-02 10:00:00",
            "summary": "新会话",
            "token_usage": _usage(prompt=5000, completion=1000, calls=3),
        },
    }
    _patch_portal(monkeypatch, SimpleNamespace(list=lambda: info))
    out = asyncio.run(main.list_sessions())
    by_id = {s["id"]: s for s in out["sessions"]}
    assert by_id["s_old"]["token_usage"] is None
    tk = by_id["s_cur"]["token_usage"]
    assert tk is not None and tk["short"] == "6k"
    assert by_id["s_cur"]["is_current"] is True


def test_search_sessions_exposes_token_usage(monkeypatch) -> None:
    """搜索端点与列表端点同口径（同一张卡片两种来源，字段不许漂）"""
    items = [
        {
            "id": "s_cur",
            "meta": {"summary": "x", "message_count": 2, "token_usage": _usage(prompt=800, completion=200, calls=1)},
            "snippets": [],
            "updated": "2026-11-02 10:00:00",
        },
        {"id": "s_old", "meta": {"summary": "y"}, "snippets": [], "updated": "2026-11-01 10:00:00"},
    ]
    _patch_portal(monkeypatch, SimpleNamespace(search=lambda q, limit: items))
    out = asyncio.run(main.search_sessions(q="x"))
    by_id = {s["id"]: s for s in out["results"]}
    assert by_id["s_cur"]["token_usage"]["short"] == "1k"
    assert by_id["s_old"]["token_usage"] is None
