"""Enter 键语义 e2e：桌面（Enter 发送） vs 触屏（Enter 换行）。

为什么单独成文件：判据是**媒体查询**（pointer: coarse / any-pointer: fine），
必须用不同 browser context 的 has_touch / is_mobile 造出真实差异——共用 conftest
的 `page` 夹具（桌面上下文）造不出来，只能自己开上下文。
"""

from __future__ import annotations

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


LIVE_USER = "#messagesContainer .msg-group:not([data-fp-history]) .user-msg"


def _sent_bubbles(pg):
    """只数**实时**用户气泡（排除历史渲染的）。

    注意：e2e 服务器是 session 级共享的真实 agent 进程，测试发出去的消息会真的
    落盘成会话历史，后续用例打开页面时会当作历史渲染出来（且是异步到达的）。
    所以判据必须按 [data-fp-history] 划界，而不是「计数 == 0」——后者会被上一个
    用例的历史坑掉，且会随历史到达的时机抖动（实测踩过两次）。"""
    return pg.evaluate("sel => document.querySelectorAll(sel).length", LIVE_USER)


def test_19_touch_enter_inserts_newline(browser, server):
    """触屏：Enter 必须插入换行（没有 Shift 键，Enter 若发送就永远写不出第二行）。

    顺带锁住提示文案口径：触屏显示「Enter 换行 · ➤ 发送」，桌面才显示「Enter 发送」。
    """
    ctx, pg = _login(browser, server, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844})
    try:
        assert pg.evaluate("() => document.documentElement.dataset.inputMode") == "touch"
        assert pg.locator(".hint-touch").is_visible()
        assert not pg.locator(".hint-keyboard").is_visible()

        base = _sent_bubbles(pg)
        inp = pg.locator("#inputField")
        inp.fill("第一行")
        inp.press("Enter")
        inp.type("第二行")
        assert inp.input_value() == "第一行\n第二行", repr(inp.input_value())
        assert _sent_bubbles(pg) == base, "触屏 Enter 不得发送（否则无法换行）"

        # 发送只能靠 ➤：点一下必须真的发出去（否则触屏就彻底发不出消息）
        pg.click("#sendBtn")
        expect(pg.locator(LIVE_USER)).to_have_count(base + 1, timeout=10000)
    finally:
        ctx.close()


def test_20_keyboard_enter_sends_and_ime_not_hijacked(browser, server):
    """桌面：Enter 发送；但输入法候选词确认（isComposing / 229）必须放行给 IME。

    IME 这一条是既有坑：中文选词回车被当成「发送」，把半截消息发了出去。
    KeyboardEventInit 里没有 isComposing，故用 defineProperty 造合成事件。
    """
    ctx, pg = _login(browser, server)
    try:
        assert pg.evaluate("() => document.documentElement.dataset.inputMode") == "keyboard"
        assert pg.locator(".hint-keyboard").is_visible()

        base = _sent_bubbles(pg)
        inp = pg.locator("#inputField")
        inp.fill("正在输入的中文")
        # 合成「输入法选词中的回车」：浏览器默认行为未被阻止 = 放行给 IME
        not_prevented = pg.evaluate(
            """() => {
                const ev = new KeyboardEvent('keydown',
                    {key: 'Enter', keyCode: 13, bubbles: true, cancelable: true});
                Object.defineProperty(ev, 'isComposing', {value: true});
                return document.getElementById('inputField').dispatchEvent(ev);
            }"""
        )
        assert not_prevented, "选词回车被 preventDefault → IME 确认键失效"
        assert _sent_bubbles(pg) == base, "输入法选词回车不得发送半截消息"

        # 真正的 Enter（非 composing）→ 发送
        inp.press("Enter")
        expect(pg.locator(LIVE_USER)).to_have_count(base + 1, timeout=10000)
        assert inp.input_value() == "", "发送后输入框必须清空"
    finally:
        ctx.close()


def test_21_input_scrollable_and_scrollpos_preserved(browser, server):
    """长草稿：输入框必须能滚，且每次 autoResize 不得把手滑到的位置弹回顶部。

    旧实现每敲一个字先 `height:auto` 再量高——这一步会把 scrollTop 夹回 0，
    长草稿里表现为「手指一拖就弹回顶部」，移动端尤其致命（没有键盘方向键兜底）。
    实测口径：溢出存在 → 手移到中段 → 触发一次 input（autoresize）→ 位置仍在。
    """
    ctx, pg = _login(browser, server, has_touch=True, is_mobile=True, viewport={"width": 390, "height": 844})
    try:
        inp = pg.locator("#inputField")
        inp.fill("\n".join(f"第 {i} 行内容" for i in range(30)))
        metrics = pg.evaluate(
            """() => {
                const el = document.getElementById('inputField');
                const cs = getComputedStyle(el);
                return {scroll: el.scrollHeight, client: el.clientHeight,
                        touchAction: cs.touchAction, overflowY: cs.overflowY,
                        maxHeight: cs.maxHeight};
            }"""
        )
        assert metrics["scroll"] > metrics["client"] + 50, f"输入框未溢出、无从滚动: {metrics}"
        # 触摸手势必须被允许（否则外层滚动容器会抢走竖向滑动）
        assert "pan-y" in metrics["touchAction"], metrics
        assert metrics["overflowY"] in ("auto", "scroll"), metrics

        # 手滑到中段 → 再触发一次 autoresize（模拟继续输入）→ 位置不得被重置
        top_after = pg.evaluate(
            """() => {
                const el = document.getElementById('inputField');
                el.scrollTop = 40;
                const before = el.scrollTop;
                el.dispatchEvent(new Event('input', {bubbles: true}));
                return {before, after: el.scrollTop};
            }"""
        )
        assert top_after["before"] == 40, top_after
        assert top_after["after"] == 40, f"autoresize 把滚动位置弹回了顶部: {top_after}"
    finally:
        ctx.close()
