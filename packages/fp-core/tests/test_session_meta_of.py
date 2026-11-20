"""SessionManager.meta_of —— 读**任意**会话的 meta，只读、不 switch、不写盘。

为什么需要这层测试：ACP 每轮结束要读「本轮会话」的标题，而 core 只有一个
当前会话。用 `meta`（property，只认当前会话）会读到别人的；用 `list_sessions()`
会扫描并**回填**全部会话文件 —— 一条读路径带写副作用。

所以这里断言三件容易被实现悄悄破坏的事（删掉 `meta_of` 的读盘分支全部必红）：
1. 非当前会话 → 读盘，拿到的是**它自己**的字段；
2. 不产生副作用：当前会话不变、会话文件字节不变（不是「看起来没变」）；
3. 畸形 sid（空 / 带分隔符 / 相对跳转）一律空字典，不许拼出目录外的路径。
"""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.cwd import reset_cwd, set_cwd
from fp_core.core.session import SessionManager


@pytest.fixture
def sessions_dir(tmp_path):
    d = str(tmp_path / "sessions")
    os.makedirs(d, exist_ok=True)
    with patch.object(cfg, "SESSIONS_DIR", d), patch.object(session_mod, "SESSIONS_DIR", d):
        yield d


@pytest.fixture
def cwd_isolated():
    token = set_cwd(os.getcwd())
    try:
        yield
    finally:
        reset_cwd(token)


@pytest.fixture
def two_sessions(sessions_dir, cwd_isolated, tmp_path):
    a = tmp_path / "projA"
    b = tmp_path / "projB"
    a.mkdir()
    b.mkdir()
    sm = SessionManager(resume=False)
    sid_a = sm.create_session(cwd=str(a))
    sid_b = sm.create_session(cwd=str(b))
    sm.switch_session(sid_a)  # 当前 = A，要读的是 B
    return sm, sid_a, sid_b, str(a), str(b)


class TestMetaOf:
    def test_reads_other_session_not_current(self, two_sessions):
        """非当前会话必须读到**它自己**的 cwd（退化实现会返回当前会话的）。

        变异敏感：把读盘分支删成 `return dict(self._meta)` → 这里拿到 A 的 cwd，必红。
        """
        sm, sid_a, sid_b, a, b = two_sessions
        assert sm.session_id == sid_a
        assert sm.meta_of(sid_b).get("cwd") == b
        assert sm.meta_of(sid_a).get("cwd") == a

    def test_does_not_switch_current_session(self, two_sessions):
        """读别人的 meta 不得有「切过去」的副作用（会打断正在进行的会话）。"""
        sm, sid_a, sid_b, _, _ = two_sessions
        sm.meta_of(sid_b)
        assert sm.session_id == sid_a, "读路径不许改当前会话"

    def test_is_read_only_on_disk(self, two_sessions):
        """必须是纯读：会话文件字节前后一致。

        对照 `list_sessions()` —— 它会回填摘要**写盘**，作为读路径代价过大。
        """
        sm, _, sid_b, _, _ = two_sessions
        path = sm.get_session_path(sid_b)
        with open(path, "rb") as f:
            before = f.read()
        sm.meta_of(sid_b)
        with open(path, "rb") as f:
            assert f.read() == before, "meta_of 不该写盘"

    def test_current_session_uses_memory(self, two_sessions):
        """当前会话走内存：**盘上文件不存在也必须读得到**。

        变异敏感：把 `if sid == self._session_id: return dict(self._meta)`
        退化成「一律读盘」，这里就只剩 {}，必红。这正是真实场景——刚
        `session/new` 的会话尚未落盘（惰性建文件），本轮结束时读它的标题。
        """
        sm, sid_a, _, _, _ = two_sessions
        os.remove(sm.get_session_path(sid_a))  # 模拟「尚未落盘」
        assert not os.path.exists(sm.get_session_path(sid_a))
        assert sm.meta_of(sid_a), "当前会话必须走内存，不能读盘"
        assert sm.meta_of(sid_a) == sm.meta

    def test_unknown_session_is_empty(self, two_sessions):
        sm, _, _, _, _ = two_sessions
        assert sm.meta_of("s_does_not_exist") == {}

    @pytest.mark.parametrize("bad", ["", "   ", "../escape", "a/b", "a\\b", ".", ".."])
    def test_rejects_unsafe_sid(self, two_sessions, bad):
        """sid 来自协议参数/HTTP path，不许拼出会话目录之外的路径。"""
        sm, _, _, _, _ = two_sessions
        assert sm.meta_of(bad) == {}
