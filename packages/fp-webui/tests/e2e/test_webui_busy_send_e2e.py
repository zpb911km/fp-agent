"""agent 忙时的发送能力 e2e：➤ 永远是发送，⏹️ 独立成键。

要锁住的不变量（每一条都对应过一次真实回归）：
1. agent 忙时 ➤ 必须仍在、且点下去发的是 ``message``（排队，见 #158），**不是** ``cancel``；
2. 中断必须另有其键（⏹️），且只有忙时才出现；
3. 触屏（Enter 只能换行）在忙时也必须能发出消息——这是本组用例存在的理由：
   曾经忙时把 ➤ 顶替成 ⏹️，桌面还有 Enter 这条暗路，触屏则**完全没有发送入口**；
4. 忙时提示文案只在触屏露（桌面 Enter 一直可用，不必解释按钮变义）。

做法：拦 ``WebSocket.prototype.send``（不转交），只记账出站帧。
这样断言的是"前端到底想发什么"，不会真的起一个 agent 回合——判据与后端实现解耦。
"""

from __future__ import annotations

import json

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


def _login(browser, server, **ctx_kwargs):
    ctx = browser.new_context(**ctx_kwargs)
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.goto(server.base_url + "/", wait_until="domcontentloaded")
    pg.fill("#authInput", server.token)
    pg.click("#authBtn")
    expect(pg.locator("#authOverlay")).to_be_hidden(timeout=20000)
    return ctx, pg


# 拦出站帧：记进 window.__sentFrames，不转交给真正的 socket。
_SPY = """() => {
    window.__sentFrames = [];
    WebSocket.prototype.send = function (d) { window.__sentFrames.push(String(d)); };
    return true;
}"""

# pong 是心跳回执，会随机混进来（拦了 send 之后服务端收不到 pong，本该断连，
# 但用例足够短）；只关心 message / cancel 这两类**用户意图**帧。
_FRAMES = """() => (window.__sentFrames || [])
    .map(s => JSON.parse(s))
    .filter(f => f.type === 'message' || f.type === 'cancel')"""


def _set_busy(pg, busy: bool):
    """用真实事件路径切忙/闲态（wire.js 的 connected 快照分支 → setProcessing）。"""
    pg.evaluate("b => window.__fp_dispatch({type: 'connected', processing: b})", busy)


def test_22_touch_send_while_busy_queues_not_cancels(browser, server):
    """触屏 + agent 忙：➤ 仍在，点它就是发消息（排队）；中断另有 ⏹️。"""
    ctx, pg = _login(browser, server, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844})
    try:
        pg.evaluate(_SPY)
        _set_busy(pg, True)

        send_btn = pg.locator("#sendBtn")
        stop_btn = pg.locator("#stopBtn")

        # 忙态下两个键并存，➤ 没被顶替
        assert send_btn.is_visible(), "agent 忙时 ➤ 消失了 → 触屏彻底发不出消息"
        assert send_btn.text_content().strip() == "➤"
        assert not send_btn.is_disabled()
        assert stop_btn.is_visible(), "忙时没有中断键 → 无法停"
        assert pg.locator("#busyHint").is_visible(), "触屏忙时必须解释 ➤ 的『排队』语义"

        # 双键挤在 390px 行里：输入框仍须有可用宽度，且整页不得被撑出横向滚动
        assert pg.evaluate("() => document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"), (
            "两个按钮把输入区撑出横向滚动"
        )
        width = pg.locator("#inputField").bounding_box()["width"]
        assert width >= 140, f"双键挤占后输入框只剩 {width}px"

        inp = pg.locator("#inputField")
        inp.fill("忙时排队的一句话")
        pg.click("#sendBtn")

        frames = pg.evaluate(_FRAMES)
        assert [f["type"] for f in frames] == ["message"], f"➤ 在忙时被误当成中断: {frames}"
        assert frames[0]["content"] == "忙时排队的一句话"
        assert inp.input_value() == "", "点发送后输入框必须清空"
        assert pg.evaluate("() => window.__fp_store.getState().processing") is True, (
            "发排队消息不得把本回合状态打成空闲"
        )

        # 中断是另一件事，由 ⏹️ 承担
        pg.click("#stopBtn")
        frames = pg.evaluate(_FRAMES)
        assert [f["type"] for f in frames] == ["message", "cancel"], frames
        assert pg.locator("#stopBtn").is_disabled(), "取消在途时中断键应置灰防连点"
        # ➤ 此时仍可用：用户取消往往正是想「停掉重说」
        assert not pg.locator("#sendBtn").is_disabled()
    finally:
        ctx.close()


def test_23_busy_keys_appear_and_retire_with_state(browser, server):
    """闲态不得露出中断键；忙→闲必须收回去（含提示文案复位）。"""
    ctx, pg = _login(browser, server, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844})
    try:
        # 初始（空闲）：只有 ➤
        assert pg.locator("#sendBtn").is_visible()
        assert pg.locator("#stopBtn").is_hidden(), "空闲时就露出中断键 → 会误点掉正常回合"
        assert pg.locator("#busyHint").is_hidden()

        _set_busy(pg, True)
        assert pg.locator("#stopBtn").is_visible()
        assert pg.locator("#busyHint").is_visible()

        _set_busy(pg, False)
        assert pg.locator("#stopBtn").is_hidden()
        assert pg.locator("#busyHint").is_hidden()
        assert pg.locator("#sendBtn").is_visible()
        assert "忙时" not in pg.locator("#sendBtn").get_attribute("title")
    finally:
        ctx.close()


def test_24_desktop_busy_enter_queues_and_hint_stays_quiet(browser, server):
    """桌面：忙时 Enter 仍然发消息（排队），且忙时提示**不出现**（那是触屏专用）。"""
    ctx, pg = _login(browser, server)
    try:
        pg.evaluate(_SPY)
        _set_busy(pg, True)

        assert pg.locator("#stopBtn").is_visible()
        assert pg.locator("#sendBtn").text_content().strip() == "➤"
        assert not pg.locator("#busyHint").is_visible(), "桌面 Enter 一直可用，忙时提示属于噪声"

        inp = pg.locator("#inputField")
        inp.fill("桌面忙时排队")
        inp.press("Enter")

        frames = pg.evaluate(_FRAMES)
        assert [f["type"] for f in frames] == ["message"], f"忙时 Enter 必须发送而非中断: {frames}"
        assert json.loads(json.dumps(frames[0]["content"])) == "桌面忙时排队"
        assert inp.input_value() == ""
    finally:
        ctx.close()
