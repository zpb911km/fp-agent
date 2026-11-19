"""本轮重放 e2e：agent 忙时刷新/重连，页面必须能重建本轮内容并继续流式。

## 为什么单独一个文件、并且换掉 WebSocket

服务端侧的契约已由 ``tests/test_webui_turn_replay.py`` 锁住（真 handler + 假 transport）。
这里要锁的是**浏览器端那一半**，两件事各自都可能单独坏掉：

  1. **去重器**：``ws.js`` 的 onmessage 有 ``seq <= conn.lastSeq → return``。
     connected 若把 lastSeq 对齐到快照点（旧行为）而不是 ``replay_base``，
     本轮重放的事件 seq 全都 ≤ 快照点 → **整批被静默丢弃**，界面依旧是老的
     「卡在一条消息上」。这条只有走真 onmessage 才测得到，``__fp_dispatch`` 会绕过它。
  2. **落位顺序**：新页面（seq=0）会同时发起 REST 历史重拉与事件重放。历史必须
     锚定容器最前、重放卡片在后——否则复现出「新消息从历史顶部往下长」的老毛病。

做法：登录（真 HTTP，无模型调用）后把 ``window.WebSocket`` 换成可控假 socket，
由测试逐帧推送。**不连真实 LLM**：这些事件本来就是服务端从总线缓冲里原样补发的，
不需要模型在场。
"""

from __future__ import annotations

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e


# 假 socket：只实现 ws.js 用到的那点接口（onopen/onmessage/readyState/send/close）。
# __push 由测试逐帧驱动，等于把服务端发来的帧原样交给真 onmessage 处理。
_FAKE_WS = """() => {
    window.__srv = { sockets: [], out: [] };
    function FakeWS(url) {
        this.url = url;
        this.readyState = 0;
        this.onopen = this.onclose = this.onerror = this.onmessage = null;
        window.__srv.sockets.push(this);
        var self = this;
        setTimeout(function () {
            if (self.readyState !== 0) return;
            self.readyState = 1;
            if (self.onopen) self.onopen({});
        }, 0);
    }
    FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
    FakeWS.prototype.send = function (d) { window.__srv.out.push(String(d)); };
    FakeWS.prototype.close = function () {
        this.readyState = 3;
        if (this.onclose) this.onclose({});
    };
    window.WebSocket = FakeWS;
    window.__push = function (obj) {
        var s = window.__srv.sockets[window.__srv.sockets.length - 1];
        if (s && s.onmessage) s.onmessage({ data: JSON.stringify(obj) });
        return true;
    };
}"""

# 上一轮（已落盘）的历史：新页面的 REST 重拉会拿到它
_OLD_HISTORY = [
    {"role": "user", "content": "上一轮的提问", "index": 1},
    {"role": "assistant", "content": "上一轮的回答", "index": 2},
]


def _boot(browser, server, **ctx_kwargs):
    """装假 socket → 打开页面 → 登录 → 等 socket 就绪。"""
    ctx = browser.new_context(**ctx_kwargs)
    # add_init_script 执行的是脚本**源码**（不是求值表达式）→ 这里把函数立即调用，
    # 否则等于往页面里塞了一个从不执行的箭头函数字面量。
    ctx.add_init_script("(" + _FAKE_WS + ")();")
    pg = ctx.new_page()
    pg.set_default_timeout(15000)

    # 本轮尚未落盘 → 真实服务端的 /messages 不含本轮；这里替身返回上一轮，
    # 这样断言能同时覆盖「历史在最前、本轮在后面」这个顺序不变量。
    def _hist(route):
        route.fulfill(json={"messages": _OLD_HISTORY})

    pg.route("**/api/sessions/*/messages", _hist)
    pg.goto(server.base_url + "/", wait_until="domcontentloaded")
    pg.fill("#authInput", server.token)
    pg.click("#authBtn")
    expect(pg.locator("#authOverlay")).to_be_hidden(timeout=20000)
    expect(pg.locator("#messagesContainer")).to_be_visible()
    pg.wait_for_function("() => (window.__srv && window.__srv.sockets.length) > 0", timeout=15000)
    pg.wait_for_function(
        "() => window.__srv.sockets[window.__srv.sockets.length - 1].readyState === 1",
        timeout=15000,
    )
    return ctx, pg


def _connected(pg, **over):
    frame = {
        "type": "connected",
        "run_id": "run-e2e",
        "seq": 20,
        "session_id": "sess-e2e",
        "replay": False,
        "resync": False,
        "processing": True,
        "pending_ask": False,
    }
    frame.update(over)
    pg.evaluate("f => window.__push(f)", frame)


# 本轮至今的事件（seq 11..15）+ 一条实时新帧（seq 21，走 push_task 的等价物）
_TURN_FRAMES = [
    {"type": "user_message", "content": "你好", "seq": 11},
    {"type": "llm_start", "seq": 12},
    {"type": "chunk", "content": "t0", "seq": 13},
    {"type": "chunk", "content": "t1", "seq": 14},
    {"type": "chunk", "content": "t2", "seq": 15},
]


def _push_all(pg, frames):
    for f in frames:
        pg.evaluate("f => window.__push(f)", f)


def _wait_assistant(pg, needle):
    """等某张助手卡片正文包含 needle（本仓 playwright 无 expect.poll，用 wait_for_function）"""
    pg.wait_for_function(
        "t => [...document.querySelectorAll('.assistant-msg .msg-content')].some(n => (n.innerText || '').includes(t))",
        arg=needle,
        timeout=15000,
    )


def _assistant_texts(pg):
    return [t.strip() for t in pg.locator(".assistant-msg .msg-content").all_inner_texts() if t.strip()]


def test_turn_replay_rebuilds_busy_turn_after_refresh(browser, server):
    """刷新页面（seq=0）而 agent 正忙 → 本轮提问/正文全部重建，并继续流式。"""
    ctx, pg = _boot(browser, server)
    try:
        # 快照点 seq=20，本轮起点 10 → 服务端会重放 11..15
        _connected(pg, replay_base=10, seq=20)
        _push_all(pg, _TURN_FRAMES)

        # 本轮提问必须出现（这是「卡在一条消息上」缺失的第一样东西）
        expect(pg.locator(".msg.user-msg .msg-content", has_text="你好")).to_have_count(1)
        # 已生成正文必须出现
        _wait_assistant(pg, "t0t1t2")

        # 随后到达的实时帧必须接在同一张卡片上（流式没断）
        pg.evaluate("f => window.__push(f)", {"type": "chunk", "content": "t3", "seq": 21})
        _wait_assistant(pg, "t0t1t2t3")

        # 收尾事件仍能正常闭合本轮
        pg.evaluate(
            "f => window.__push(f)",
            {
                "type": "llm_end",
                "content": "t0t1t2t3",
                "has_tool_calls": False,
                "seq": 22,
            },
        )
        _wait_assistant(pg, "t0t1t2t3")

        # 顺序不变量：历史（REST 后到）必须锚定在最前，本轮内容全在其后
        ok = pg.evaluate("""() => {
            const c = document.getElementById('messagesContainer');
            const kids = [...c.children].filter(n => !n.hasAttribute('data-fp-loading'));
            const hist = kids.filter(n => n.hasAttribute('data-fp-history'));
            if (!hist.length) return 'no-history';
            const lastHistIdx = Math.max(...hist.map(n => kids.indexOf(n)));
            const firstLiveIdx = Math.min(...kids
                .map((n, i) => n.hasAttribute('data-fp-history') ? -1 : i)
                .filter(i => i >= 0));
            return lastHistIdx < firstLiveIdx ? 'ok' : ('history-after-live:' + lastHistIdx + '>' + firstLiveIdx);
        }""")
        assert ok == "ok", f"历史应整体位于本轮实时内容之前，实测 {ok}"
        assert "上一轮的回答" in pg.locator("#messagesContainer").inner_text()
    finally:
        ctx.close()


def test_without_replay_base_turn_frames_are_deduped(browser, server):
    """反证：connected 不携带 replay_base（修复前的帧）→ 同样这批帧会被去重器丢掉。

    服务端若只补发事件、不告知起点，前端 lastSeq 会被对齐到快照点 20，
    而重放事件 seq 11..15 全都 ≤ 20 → onmessage 直接 return，界面什么都看不到。
    本用例证明测的是**真去重器**，不是 dispatch 旁路。
    """
    ctx, pg = _boot(browser, server)
    try:
        _connected(pg, seq=20)  # 无 replay_base（旧协议）
        _push_all(pg, _TURN_FRAMES)
        pg.wait_for_timeout(300)  # 给异步渲染留时间（本应什么都不发生）
        assert pg.locator(".msg.user-msg .msg-content", has_text="你好").count() == 0
        assert "t0t1t2" not in "".join(_assistant_texts(pg))
    finally:
        ctx.close()
