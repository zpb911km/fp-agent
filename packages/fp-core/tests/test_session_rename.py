"""会话重命名（SessionManager.set_summary / portal ctl.sessions.rename）

契约（webui 侧栏「✎ 重命名」与终端共同依赖）：

  1. **写入即锁定**：``meta[summary_locked]`` 置位后，三条自动派生路径
     （``save_context`` / ``save_message`` / ``save_and_summarize``）全部让路。
     不锁的话用户改的名字会被**下一条消息**静默改回派生值——「改了但没改」。
  2. **空串 = 撤销**：解除锁定并回到「末条 user 消息」派生值。
  3. **不动 updated**：改名不该让会话在列表里（按 updated 排序）跳到最前面。
  4. **不造文件**：惰性会话只改内存；不存在的 sid → None，盘上不留痕。
  5. 规范化：换行折叠、超 ``SUMMARY_LIMIT`` 截断（与自动派生同一上限）。
"""

import json
import os
from unittest.mock import patch

import pytest

import fp_core.config as cfg
import fp_core.core.session as session_mod
from fp_core.core.session import SUMMARY_LIMIT, SUMMARY_LOCKED, SessionManager


@pytest.fixture
def sessions_dir(tmp_path):
    """临时会话目录（patch 所有引用点，隔离真实 ~/.fp/sessions）"""
    d = str(tmp_path / "sessions")
    os.makedirs(d, exist_ok=True)

    with patch.object(cfg, "SESSIONS_DIR", d), patch.object(session_mod, "SESSIONS_DIR", d):
        yield d


def _meta_of(sid: str) -> dict:
    path = os.path.join(session_mod.SESSIONS_DIR, f"{sid}.jsonl")
    with open(path, encoding="utf-8") as f:
        return json.loads(f.readline())


def _make_session(text: str = "帮我查个东西") -> tuple[SessionManager, str]:
    """建一个已落盘的当前会话，返回 (manager, sid)"""
    sm = SessionManager(resume=False)
    sm.save_message("user", text)
    return sm, sm.session_id


class TestSetSummaryBasics:
    """写入、规范化、返回値"""

    def test_rename_current_session(self, sessions_dir):
        """当前会话改名 → 盘上 summary 与锁均落盘"""
        sm, sid = _make_session()

        assert sm.set_summary(sid, "  重构   会话模块  ") == "重构 会话模块"
        meta = _meta_of(sid)
        assert meta["summary"] == "重构 会话模块"
        assert meta[SUMMARY_LOCKED] is True
        assert sm.meta["summary"] == "重构 会话模块"  # 内存同步，后续 save_context 不覆盖

    def test_rename_other_session(self, sessions_dir):
        """任意 sid（非当前会话）也能改名——webui 列表里点别人那条"""
        sm, current = _make_session()
        sm.save_and_summarize([{"role": "user", "content": "另一个会话的内容"}])
        other = sm.create_session()
        sm.save_message("user", "这是另一个会话")
        sm.switch_session(current)

        assert sm.set_summary(other, "另一个会话的名字") == "另一个会话的名字"
        assert _meta_of(other)["summary"] == "另一个会话的名字"
        # 当前会话不受影响
        assert _meta_of(current)["summary"] != "另一个会话的名字"

    def test_newlines_collapsed(self, sessions_dir):
        """换行/多空白折叠成单空格（列表一行一条，路标不能断行）"""
        sm, sid = _make_session()
        assert sm.set_summary(sid, "第一行\n第二行\t\t第三行") == "第一行 第二行 第三行"

    def test_too_long_clipped(self, sessions_dir):
        """超上限按 SUMMARY_LIMIT 截断加省略号（与自动派生同一上限）"""
        sm, sid = _make_session()
        out = sm.set_summary(sid, "长" * 40)

        assert out is not None
        assert len(out) == SUMMARY_LIMIT
        assert out.endswith("…")
        assert _meta_of(sid)["summary"] == out

    def test_updated_timestamp_untouched(self, sessions_dir):
        """改名不动 updated —— 会话不该因改名跳到列表最前面"""
        sm, sid = _make_session()
        before = _meta_of(sid)["updated"]

        sm.set_summary(sid, "新名字")

        assert _meta_of(sid)["updated"] == before


class TestLockSemantics:
    """锁定后自动派生必须让路（本模块存在的理由）"""

    def test_save_message_does_not_overwrite(self, sessions_dir):
        """再发 user 消息（save_message → _refresh_summary）不覆盖人工名"""
        sm, sid = _make_session()
        sm.set_summary(sid, "人工命名")

        sm.save_message("user", "我又发了一条完全不同的需求")

        assert _meta_of(sid)["summary"] == "人工命名"

    def test_save_context_does_not_overwrite(self, sessions_dir):
        """整段落盘（save_context，生产主路径）不覆盖人工名"""
        sm, sid = _make_session()
        sm.set_summary(sid, "人工命名")

        sm.save_context([
            {"role": "user", "content": "第一条"},
            {"role": "assistant", "content": "回复"},
            {"role": "user", "content": "换一个话题"},
        ])

        assert _meta_of(sid)["summary"] == "人工命名"

    def test_save_and_summarize_does_not_overwrite(self, sessions_dir):
        """切换/退出路径（save_and_summarize）不覆盖，且返回人工名"""
        sm, sid = _make_session()
        sm.set_summary(sid, "人工命名")

        msgs = [{"role": "user", "content": "退出前最后一句完全不同的需求"}]
        assert sm.save_and_summarize(msgs, sid) == "人工命名"
        assert _meta_of(sid)["summary"] == "人工命名"

    def test_lock_survives_restart(self, sessions_dir):
        """锁存在会话 meta 里 → 新进程 resume 后依然生效"""
        sm, sid = _make_session()
        sm.set_summary(sid, "重启后仍在")

        sm2 = SessionManager(resume=sid)
        sm2.save_message("user", "新进程里发的一条消息")

        assert _meta_of(sid)["summary"] == "重启后仍在"

    def test_list_sessions_reports_locked_name(self, sessions_dir):
        """列表口径同源：list_sessions() 返回人工名，不回填派生值"""
        sm, sid = _make_session()
        sm.set_summary(sid, "列表里看到的名字")

        assert sm.list_sessions()[sid]["summary"] == "列表里看到的名字"


class TestRevokeAndMissing:
    """撤销人工命名 / 不存在的会话"""

    def test_empty_summary_revokes_lock(self, sessions_dir):
        """空串 = 撤销：解锁并回到末条 user 派生值"""
        sm, sid = _make_session("原始需求文本")
        sm.set_summary(sid, "人工命名")

        out = sm.set_summary(sid, "   ")

        assert out == "原始需求文本"  # 立刻重算，不闪「(无摘要)」
        meta = _meta_of(sid)
        assert SUMMARY_LOCKED not in meta
        assert meta["summary"] == "原始需求文本"

        # 解锁后自动派生恢复工作
        sm.save_message("user", "撤销后的新需求")
        assert _meta_of(sid)["summary"] == "撤销后的新需求"

    def test_missing_session_returns_none(self, sessions_dir):
        """不存在的 sid → None，且绝不创建文件"""
        sm = SessionManager(resume=False)
        assert sm.set_summary("s_260101_000000000001", "无主名字") is None
        assert not os.path.exists(os.path.join(session_mod.SESSIONS_DIR, "s_260101_000000000001.jsonl"))

    def test_path_traversal_rejected(self, sessions_dir):
        """带路径分隔符的 sid 一律拒绝（webui 端点直收 HTTP path）"""
        sm, sid = _make_session()
        for bad in ("../evil", "a/b", "..", ""):
            assert sm.set_summary(bad, "x") is None

    def test_unflushed_current_session_stays_in_memory(self, sessions_dir):
        """惰性会话（当前会话尚无文件）：只改内存，不凭空造 0 长度文件"""
        sm = SessionManager(resume=False)  # 不落任何消息
        sid = sm.session_id
        assert not os.path.exists(sm.get_session_path())

        assert sm.set_summary(sid, "新会话的名字") == "新会话的名字"
        assert not os.path.exists(sm.get_session_path())
        assert sm.meta["summary"] == "新会话的名字"

        # 名字随首条消息一起落盘
        sm.save_message("user", "第一条消息")
        assert _meta_of(sid)["summary"] == "新会话的名字"
