"""测试 session_keywords — KL 关键词管线（临时目录隔离，零网络）"""

from __future__ import annotations

import json
import os

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
import fp_core.core.session_keywords as kw_mod
from fp_core.core.session import SessionManager
from fp_core.core.session_keywords import (
    KW_VERSION,
    compute,
    load_background,
    needs_rebuild,
    read_session,
    rebuild_all,
    save_bucket,
    score,
    tokenize,
    update_session,
)


@pytest.fixture
def sessions_dir(tmp_path):
    d = str(tmp_path / "sessions")
    os.makedirs(d, exist_ok=True)
    with (
        pytest.MonkeyPatch.context() as mp,
    ):
        mp.setattr(cfg, "SESSIONS_DIR", d)
        mp.setattr(session_mod, "SESSIONS_DIR", d)
        mp.setattr(kw_mod.config, "SESSIONS_DIR", d)
        yield d


def _write_session(d: str, sid: str, msgs: list[dict], **meta) -> str:
    path = os.path.join(d, f"{sid}.jsonl")
    header = {
        "__meta__": True,
        "id": sid,
        "created": "2026-10-20 10:00:00",
        "updated": "2026-10-20 10:00:00",
        "summary": "",
        "message_count": len(msgs),
    }
    header.update(meta)
    with open(path, "w", encoding="utf-8") as f:
        f.write(json.dumps(header, ensure_ascii=False) + "\n")
        for msg in msgs:
            f.write(json.dumps(msg, ensure_ascii=False) + "\n")
    return path


def _msgs(*texts: str) -> list[dict]:
    return [{"role": "user", "content": t} for t in texts]


class TestTokenize:
    def test_keeps_cjk_drops_code_and_stopwords(self):
        cnt = tokenize(["请帮我看看 量子退火算法 `def foo_bar():` 的 实现"])
        assert "量子" in cnt or "退火" in cnt
        joined = "".join(cnt)
        assert "foo" not in joined  # 标识符不进词表
        assert "的" not in cnt  # 虚词被过滤

    def test_code_block_excluded(self):
        """代码块不是中文 → 天然排除，无需专门清洗。"""
        cnt = tokenize(["```python\nimport os\nprint(1)\n```"])
        assert not cnt

    def test_bigram_fallback_without_jieba(self, monkeypatch):
        """jieba 不可用时退化为 bigram，功能不中断。"""
        monkeypatch.setattr(kw_mod, "_jieba_mod", False)
        cnt = tokenize(["量子退火优化调度"])
        assert cnt
        assert any(len(w) == 2 for w in cnt)

    def test_injected_prefix_detected(self):
        assert kw_mod.is_injected("⁂[job_done] 后台任务完成")
        assert kw_mod.is_injected("[背景信息] 你是一个助手")
        assert not kw_mod.is_injected("帮我修一下这个 bug")


class TestScore:
    def test_empty_background_degrades_to_frequency(self):
        cnt = {"番茄": 5, "鸡蛋": 2}
        ranked = score(cnt, {}, 0, {}, 0, top_k=2)
        assert ranked == ["番茄", "鸡蛋"]

    def test_distinctive_word_beats_common_word(self):
        """背景语料里到处都是「项目」，它在本文档里再多也不该排第一。"""
        import collections

        bg = collections.Counter({"项目": 1000, "会议": 800})
        cnt = collections.Counter({"项目": 20, "番茄": 8})
        ranked = score(cnt, bg, sum(bg.values()), collections.Counter(), 0, top_k=2)
        assert ranked[0] == "番茄"

    def test_empty_doc(self):
        assert score({}, {}, 0, {}, 0) == []

    def test_df_upper_bound_filters_background_words(self):
        """出现在 >10% 会话里的词是背景噪声，应被剔除（需 n>=10 才启用）。"""
        import collections

        cnt = collections.Counter({"项目": 10})
        gdf = collections.Counter({"项目": 50})  # 50/100 = 50% > DF_MAX
        assert score(cnt, {}, 0, gdf, 100) == []

    def test_suppress_set_applied(self):
        cnt = {"番茄": 5, "鸡蛋": 3}
        assert score(cnt, {}, 0, {}, 0, suppress=frozenset({"番茄"})) == ["鸡蛋"]


class TestBuckets:
    def test_save_and_load_background(self, sessions_dir):
        import collections
        import datetime

        day = datetime.date(2026, 10, 20)
        save_bucket(day, collections.Counter({"甲": 3}), collections.Counter({"甲": 1}), 1, sessions_dir)
        tc, total, gdf, n = load_background(day, sessions_dir)
        assert tc["甲"] == 3 and total == 3 and gdf["甲"] == 1 and n == 1

    def test_window_excludes_old_buckets(self, sessions_dir):
        import collections
        import datetime

        old = datetime.date(2026, 1, 1)
        today = datetime.date(2026, 10, 20)
        save_bucket(old, collections.Counter({"陈年": 5}), collections.Counter({"陈年": 1}), 1, sessions_dir)
        tc, _, _, _ = load_background(today, sessions_dir)
        assert "陈年" not in tc  # 超出 21 天窗口

    def test_needs_rebuild_when_state_missing(self, sessions_dir):
        assert needs_rebuild(sessions_dir) is True


class TestCompute:
    def test_returns_keywords_for_content(self, sessions_dir):
        path = _write_session(sessions_dir, "s_261020_100000000001", _msgs("量子退火算法怎么实现", "再讲一次量子退火"))
        kws = compute(path, sessions_dir)
        assert kws
        assert len(kws) <= 3

    def test_fork_skips_shared_prefix(self, sessions_dir):
        """fork 会话只统计分叉点之后的内容——共享前缀是关键词扎堆的根因。"""
        shared = _msgs("西红柿炒蛋的做法") * 3
        path = _write_session(
            sessions_dir,
            "s_261020_100000000002",
            shared + _msgs("顺便看看量子退火排程"),
            fork_from={"sid": "s_parent", "msg_count": len(shared)},
        )
        kws = compute(path, sessions_dir)
        assert "量子" in "".join(kws) or "退火" in "".join(kws)
        assert "西红柿" not in "".join(kws)

    def test_empty_session_returns_empty(self, sessions_dir):
        path = _write_session(sessions_dir, "s_261020_100000000003", [])
        assert compute(path, sessions_dir) == []


class TestUpdateSession:
    def test_writes_kw_into_meta(self, sessions_dir):
        path = _write_session(sessions_dir, "s_261020_100000000004", _msgs("量子退火算法排程"))
        kws = update_session("s_261020_100000000004", path, sessions_dir)
        assert kws
        meta, _ = read_session(path)
        assert meta["kw"] == kws
        assert meta["kw_v"] == KW_VERSION

    def test_body_preserved_after_write(self, sessions_dir):
        path = _write_session(sessions_dir, "s_261020_100000000005", _msgs("量子退火", "第二句"))
        update_session("s_261020_100000000005", path, sessions_dir)
        _, msgs = read_session(path)
        assert len(msgs) == 2  # 只改首行，正文不动


class TestRebuildAll:
    def test_end_to_end(self, sessions_dir):
        _write_session(sessions_dir, "s_261020_100000000010", _msgs("量子退火排程算法") * 4)
        _write_session(sessions_dir, "s_261020_100000000011", _msgs("西红柿炒蛋放多少糖") * 4)
        stats = rebuild_all(sessions_dir)
        assert stats["sessions"] == 2
        assert stats["written"] == 2
        for sid in ("s_261020_100000000010", "s_261020_100000000011"):
            meta, _ = read_session(os.path.join(sessions_dir, f"{sid}.jsonl"))
            assert meta.get("kw_v") == KW_VERSION


class TestSessionManagerIntegration:
    """落盘路径自动刷新关键词（列表只读，不算）"""

    def test_save_and_summarize_populates_kw(self, sessions_dir):
        sm = SessionManager()
        msgs = _msgs("量子退火算法怎么实现？")
        sm.save_and_summarize(msgs)
        meta, _ = read_session(os.path.join(sessions_dir, f"{sm.session_id}.jsonl"))
        assert meta.get("kw_v") == KW_VERSION
        assert isinstance(meta.get("kw"), list)

    def test_list_sessions_does_not_compute(self, sessions_dir, monkeypatch):
        """列表渲染必须只读现成字段：谁要是在这里实算，900+ 会话会拖成秒级。"""
        _write_session(sessions_dir, "s_261020_100000000020", _msgs("量子退火排程"))
        called = {"n": 0}

        def _boom(*a, **k):
            called["n"] += 1
            raise AssertionError("list_sessions 不得触发关键词计算")

        monkeypatch.setattr(kw_mod, "compute", _boom)
        monkeypatch.setattr(kw_mod, "compute_from_messages", _boom)
        sessions = SessionManager().list_sessions()
        assert sessions  # 列表正常返回
        assert called["n"] == 0
