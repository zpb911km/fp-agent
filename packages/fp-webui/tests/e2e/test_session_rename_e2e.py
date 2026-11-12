"""WebUI 会话重命名 e2e：`⋯` → `✎ 重命名` → 就地编辑 → `Enter` 保存。

隔离策略：直接在 SESSIONS_DIR 造一个独立会话文件（不调 LLM、不动当前会话、
不改任何既有会话），用例结束删除。断言两层：DOM（标题变、`data-locked`）
+ 后端（`GET /api/sessions` 的 `summary` / `summary_locked`），并验证
「清空保存 = 撤销锁定」这条反向路径——它是这套设计的另一半：
只测「能改」不测「能撤」，锁就成了单向不可逆。
"""

from __future__ import annotations

import json
import time
import urllib.request
from pathlib import Path

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# 造出来的会话里最后一条 user 消息 → 撤销锁定后由后端派生回来的自动摘要
_LAST_USER = "撤销后应回到这条原始消息"
_NEW_NAME = "e2e 人工标题"


def _sessions_dir() -> Path:
    """**服务器子进程**的会话目录。

    不能读 ``fp_core.core.session.SESSIONS_DIR``：根 conftest 的 autouse fixture
    （``_isolate_sessions_dir``）把测试进程的这个常量 patch 到 tmp_path，而 e2e
    服务器是**子进程**，不继承该 patch，用的是真实数据目录。用 ``get_data_dir()``
    现算（它本身不读被 patch 的 ``_FP_DATA_DIR``），两边才指同一处；否则造出的
    会话文件服务器根本看不见（实测症状：卡片永远不出现）。
    """
    from fp_core.config import get_data_dir

    d = Path(get_data_dir()) / "sessions"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _make_session(tmp_title: str = "e2e 原始标题") -> str:
    """造一个真实会话文件（meta 行 + 两条消息行），返回 sid。

    sid 必须满足核内 ``SID_PATTERN = ^s_\\d{6}_\\d{12,}.*\\.jsonl$``（日期 6 位 +
    数字段 ≥12 位，即秒 6 位 + 微秒 6 位）。位数不足的文件会被 ``list_sessions``
    整条跳过——实测症状是卡片永远不出现，与「前端没刷新」几乎无法区分。
    """
    from datetime import datetime

    sid = datetime.now().strftime("s_%y%m%d_%H%M%S%f")  # 与 _generate_sid 同形
    meta = {
        "__meta__": True,
        "id": sid,
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "updated": time.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": tmp_title,
        "message_count": 2,
    }
    lines = [
        json.dumps(meta, ensure_ascii=False),
        json.dumps({"role": "user", "content": _LAST_USER}, ensure_ascii=False),
        json.dumps({"role": "assistant", "content": "ok"}, ensure_ascii=False),
    ]
    (_sessions_dir() / f"{sid}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return sid


def _drop_session(sid: str) -> None:
    for p in (_sessions_dir() / f"{sid}.jsonl", _sessions_dir() / f"{sid}.jsonl.metatmp"):
        if p.exists():
            p.unlink()


def _api(server, path: str) -> dict:
    req = urllib.request.Request(server.base_url + path, headers={"Authorization": "Bearer " + server.token})
    with urllib.request.urlopen(req, timeout=10) as resp:
        return json.load(resp)


def _card_summary(server, sid: str) -> dict:
    for s in _api(server, "/api/sessions")["sessions"]:
        if s["id"] == sid:
            return s
    raise AssertionError(f"会话 {sid} 不在 /api/sessions 列表里")


def test_rename_session_inline_then_revert(server, page):
    """改标题 → 锁定生效（DOM + 后端）→ 清空保存 → 撤销回自动摘要。"""
    sid = _make_session()
    try:
        page.reload(wait_until="domcontentloaded")  # 列表重拉；token 存 localStorage，无需再登录
        expect(page.locator("#authOverlay")).to_be_hidden(timeout=20000)

        card = page.locator(f'.session-item[data-sid="{sid}"]')
        expect(card).to_be_visible(timeout=15000)
        expect(card.locator(".session-item-summary")).to_have_text("e2e 原始标题")
        assert card.get_attribute("data-locked") in ("", None), "新建会话不应带锁定标记"

        # ① 进入编辑：菜单 → ✎
        card.locator(".session-more-btn").click()
        card.locator(".session-rename-btn").click()
        inp = card.locator(".session-rename-input")
        expect(inp).to_be_visible()
        assert inp.input_value() == "", "未锁定的会话应空预填（避免把自动摘要当自己起的名字）"

        # ①' 上限来自后端下发（前端不写死第二个数字）
        limit = _api(server, "/api/sessions")["summary_limit"]
        assert inp.get_attribute("maxlength") == str(limit)

        # ② 保存
        inp.fill(_NEW_NAME)
        inp.press("Enter")
        expect(card.locator(".session-item-summary")).to_have_text(_NEW_NAME)
        expect(card).to_have_attribute("data-locked", "1")

        # ③ 后端已落盘并锁定
        row = _card_summary(server, sid)
        assert row["summary"] == _NEW_NAME
        assert row["summary_locked"] is True

        # ④ 再编辑：锁定的会话应预填当前名字（便于微调）
        card.locator(".session-more-btn").click()
        card.locator(".session-rename-btn").click()
        inp = card.locator(".session-rename-input")
        expect(inp).to_be_visible()
        assert inp.input_value() == _NEW_NAME

        # ⑤ 清空保存 = 撤销锁定，回到「末条 user 前 20 字」
        inp.fill("")
        inp.press("Enter")
        expect(card.locator(".session-item-summary")).to_have_text(_LAST_USER)
        expect(card).to_have_attribute("data-locked", "")

        row = _card_summary(server, sid)
        assert row["summary"] == _LAST_USER
        assert row["summary_locked"] is False
    finally:
        _drop_session(sid)


def test_rename_esc_cancels_without_touching_backend(server, page):
    """`Esc` 只还原显示，不发请求（否则用户按 Esc 反而改了名字）。"""
    sid = _make_session()
    try:
        page.reload(wait_until="domcontentloaded")
        expect(page.locator("#authOverlay")).to_be_hidden(timeout=20000)
        card = page.locator(f'.session-item[data-sid="{sid}"]')
        expect(card).to_be_visible(timeout=15000)

        card.locator(".session-more-btn").click()
        card.locator(".session-rename-btn").click()
        inp = card.locator(".session-rename-input")
        expect(inp).to_be_visible()
        inp.fill("不该保存的名字")
        inp.press("Escape")

        expect(card.locator(".session-item-summary")).to_have_text("e2e 原始标题")
        row = _card_summary(server, sid)
        assert row["summary"] == "e2e 原始标题"
        assert row["summary_locked"] is False
    finally:
        _drop_session(sid)
