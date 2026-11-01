#!/usr/bin/env python3
"""WebUI 消息卡片 E2E（手动）— 命令轮不得吞掉上一轮 assistant 卡片。

背景（回归来源）：
  流式轮 `_stream` 在轮次结束后**不销毁**（迟到 token 要并入本轮元素），
  而命令轮（/sc、/task…）不发 `llm_start`，不会重置 `_stream`；
  旧代码 `done` 直接 `finalizeStreamRound(done.final_content)`，于是命令输出被写进
  **上一轮那张卡**——卡片内容被覆盖（"吞卡"），刷新走 REST 历史才恢复。
  修法见 `ui.js` 的 `finalizeStreamRoundForDone`（done 只承接本轮卡片）。

为什么是独立脚本而不是 pytest：
  fp-webui 只依赖断言源文本的测试，没有浏览器测试设施；Playwright 不在依赖里。
  本脚本手工运行即可（静态目录起 http.server + 注入 FakeWebSocket/fetch 桩，
  用真实入口 `ws.onmessage` 重放后端事件序列），不参与 CI 门禁。

用法：
  pip install playwright && playwright install chromium
  python scripts/e2e_webui_stream_cards.py
"""

from __future__ import annotations

import functools
import http.server
import re
import socket
import sys
import threading
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
STATIC_PARENT = REPO / "packages" / "fp-webui" / "src" / "fp_webui"

ANSWER1 = "这是第一轮的完整答案正文"

INIT_JS = r"""
(() => {
  class FakeWS {
    constructor(url) {
      this.url = url; this.readyState = 0; window.__ws = this;
      setTimeout(() => { this.readyState = 1; if (this.onopen) this.onopen(); }, 0);
    }
    send(d) {}
    close() { this.readyState = 3; if (this.onclose) this.onclose({}); }
  }
  FakeWS.CONNECTING = 0; FakeWS.OPEN = 1; FakeWS.CLOSING = 2; FakeWS.CLOSED = 3;
  window.WebSocket = FakeWS;
  window.__emit = (obj) => {
    if (window.__ws && window.__ws.onmessage) window.__ws.onmessage({ data: JSON.stringify(obj) });
  };
  sessionStorage.setItem('webui_token', 'e2e-token');
  window.fetch = async (url) => {
    const u = String(url);
    let body = {};
    if (u.indexOf('/api/health') >= 0) body = { agent: 'e2e-model', session: 'sess0001' };
    else if (u.indexOf('/messages') >= 0) body = { messages: [] };
    else if (u.indexOf('/api/sessions') >= 0) body = { sessions: [] };
    else if (u.indexOf('/api/commands') >= 0) body = { commands: [] };
    return new Response(JSON.stringify(body), {
      status: 200, headers: { 'Content-Type': 'application/json' } });
  };
})();
"""

RESULTS: list[tuple[str, bool, str]] = []


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _serve() -> tuple[http.server.ThreadingHTTPServer, int]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(STATIC_PARENT))
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    srv.log_message = lambda *a, **k: None  # type: ignore[method-assign]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, srv.server_address[1]


def _emit(page, ev: dict) -> None:
    page.evaluate("o => __emit(o)", ev)


def _text(page) -> str:
    return page.evaluate("document.getElementById('messagesContainer').innerText")


def _cards(page) -> list[str]:
    return page.evaluate(
        "Array.from(document.querySelectorAll('.assistant-msg .msg-content'))"
        ".map(function(e){ return e.innerText.trim(); })"
    )


def _boot(page, url: str) -> None:
    page.goto(url, wait_until="load")
    page.wait_for_timeout(250)
    _emit(
        page,
        {
            "type": "connected",
            "run_id": "r1",
            "seq": 0,
            "session_id": "sess0001",
            "processing": False,
            "replay": False,
            "resync": False,
        },
    )
    page.wait_for_timeout(150)


def _text_turn(page, question: str, answer: str, non_system: int = 2) -> None:
    """一轮普通问答：有思考 + 正文流式"""
    _emit(page, {"type": "user_message", "content": question})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "thinking_content", "token": "先定位到 app.js"})
    _emit(page, {"type": "chunk", "content": answer})
    page.wait_for_timeout(220)  # 100ms 正文节流 flush
    _emit(page, {"type": "llm_end", "content": answer})
    _emit(page, {"type": "done", "session_id": "sess0001", "final_content": answer, "non_system_count": non_system})
    page.wait_for_timeout(150)


def _command_turn(page, cmd: str, output: str, non_system: int = 2) -> None:
    """一轮命令：后端只发 user_message(ephemeral) + done，没有 llm_start"""
    _emit(page, {"type": "user_message", "content": cmd, "ephemeral": True})
    _emit(page, {"type": "done", "session_id": "sess0001", "final_content": output, "non_system_count": non_system})
    page.wait_for_timeout(200)


def _check(name: str, cond: bool, detail: str = "") -> None:
    RESULTS.append((name, cond, detail))
    print(("  ✅ " if cond else "  ❌ ") + name + (("\n     " + detail) if detail and not cond else ""))


def case_command_after_text(page, url: str) -> None:
    """本 bug 的正身：/sc 的输出不得覆盖上一轮答案"""
    _boot(page, url)
    _text_turn(page, "帮我看看这个 bug", ANSWER1)
    _command_turn(page, "/sc", "✅ 已短路 3 个块，节省 1200 字符")
    cards = _cards(page)
    print("    assistant 卡片:", cards)
    _check("命令轮：上一轮答案仍在", ANSWER1 in _text(page))
    _check("命令轮：命令输出自成一卡", "✅ 已短路 3 个块，节省 1200 字符" in cards)
    _check("命令轮：答案卡未被改写", any(ANSWER1 in c for c in cards))


def case_normal_turn_no_dup(page, url: str) -> None:
    """回归护栏：llm_end 已渲染的正文，done 不得再渲染一次"""
    _boot(page, url)
    _text_turn(page, "你好", ANSWER1)
    cards = _cards(page)
    print("    assistant 卡片:", cards)
    _check("普通轮：只有一张答案卡", len(cards) == 1, str(cards))
    _check("普通轮：内容正确", bool(cards) and ANSWER1 in cards[0])


def case_tool_turn(page, url: str) -> None:
    """工具轮：工具前说明 / 工具卡 / 最终答案三卡并存，互不覆盖"""
    _boot(page, url)
    _emit(page, {"type": "user_message", "content": "查一下 git log"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "chunk", "content": "我先查一下历史"})
    page.wait_for_timeout(220)
    _emit(page, {"type": "llm_end", "content": "我先查一下历史", "has_tool_calls": True})
    _emit(page, {"type": "tool_select", "tools": ["bash"]})
    _emit(page, {"type": "tool_call", "name": "bash", "args": '{"command":"git log"}', "tool_call_id": "t1"})
    _emit(page, {"type": "tool_result", "name": "bash", "result": "ba0ace3 fix(tui) ...", "tool_call_id": "t1"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "chunk", "content": "最终答案在这里"})
    page.wait_for_timeout(220)
    _emit(page, {"type": "llm_end", "content": "最终答案在这里"})
    _emit(page, {"type": "done", "session_id": "sess0001", "final_content": "最终答案在这里", "non_system_count": 5})
    page.wait_for_timeout(200)
    text = _text(page)
    print("    卡片:", _cards(page))
    _check("工具轮：工具前的说明仍在", "我先查一下历史" in text)
    _check("工具轮：最终答案仍在", "最终答案在这里" in text)
    _check("工具轮：工具结果未被覆盖", "ba0ace3" in text)


def case_cancel_then_command(page, url: str) -> None:
    """中断轮（cancelled）同样终结轮次：随后的命令不得吞掉半截正文"""
    _boot(page, url)
    _emit(page, {"type": "user_message", "content": "写个长文"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "chunk", "content": "写到一半就断了"})
    page.wait_for_timeout(220)
    _emit(page, {"type": "cancelled"})
    page.wait_for_timeout(120)
    _command_turn(page, "/sc", "✅ 已短路 2 个块")
    _check("中断后命令：被中断的正文仍在", "写到一半就断了" in _text(page))
    _check("中断后命令：命令输出自成一卡", "✅ 已短路 2 个块" in _text(page))


def case_two_commands(page, url: str) -> None:
    """连发两条命令（都不发 llm_start）"""
    _boot(page, url)
    _text_turn(page, "先聊两句", ANSWER1)
    _command_turn(page, "/sc list", "块 1 · 块 2 · 块 3")
    _command_turn(page, "/sc", "✅ 已短路 1 个块")
    text = _text(page)
    _check("连发两条命令：答案仍在", ANSWER1 in text)
    _check("连发两条命令：两条输出都在", "块 1 · 块 2 · 块 3" in text and "✅ 已短路 1 个块" in text)


def case_resync_midturn(page, url: str) -> None:
    """重连 resync（缓冲溢出）会 clearUI：卡已随 DOM 作废，终稿不得写进幽灵卡"""
    _boot(page, url)
    _emit(page, {"type": "user_message", "content": "写个长文"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "chunk", "content": "半截正文"})
    page.wait_for_timeout(220)
    # 缓冲溢出 + 任务仍在跑 → clearUI 后不补历史（!data.processing 才 fetch）
    _emit(
        page,
        {
            "type": "connected",
            "run_id": "r1",
            "seq": 99,
            "session_id": "sess0001",
            "processing": True,
            "replay": False,
            "resync": True,
        },
    )
    page.wait_for_timeout(120)
    _emit(page, {"type": "done", "session_id": "sess0001", "final_content": "完整终稿在这里", "non_system_count": 2})
    page.wait_for_timeout(200)
    _check("resync 后终稿仍可见（没写进幽灵卡）", "完整终稿在这里" in _text(page))


def _think_state(page) -> list[dict]:
    """每张卡片的思考栏状态：c=折叠 / e=展开 / - 无思考块"""
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('.assistant-msg')).map((m, i) => {
            const t = m.querySelector('.think-msg');
            const b = t ? t.querySelector('.think-msg-body') : null;
            return {card: i,
                    state: t ? (t.classList.contains('collapsed') ? 'c' : 'e') : '-',
                    h: b ? Math.round(b.getBoundingClientRect().height) : -1,
                    chars: b ? (b.textContent || '').length : -1};
        })"""
    )


def _last_think(page) -> dict:
    """最后一张带思考栏的卡片的状态（工具卡可能追加在新卡上，不能只看末尾卡）"""
    st = [c for c in _think_state(page) if c["state"] != "-"]
    return st[-1] if st else {"card": -1, "state": "-", "h": -1, "chars": -1}


def _think_status(page) -> str:
    """最后一张思考栏的状态行文本"""
    return page.evaluate(
        "() => { var ts = document.querySelectorAll('.assistant-msg .think-msg');"
        " if (!ts.length) return '';"
        " var s = ts[ts.length - 1].querySelector('.think-msg-status');"
        " return s ? (s.textContent || '') : ''; }"
    )


def _status_secs(txt: str) -> float:
    m = re.search(r"([\d.]+)s", txt or "")
    return float(m.group(1)) if m else -1.0


def case_think_status_live(page, url: str) -> None:
    """思考字数/时长随思考逐步走表；折叠后定格，且与块内全文一致。

    旧实现只在折叠那一刻写一次状态行：思考期间状态行是空的（看不到字数在涨），
    折叠后迟到的思考 token 又把正文块撑大，状态行字数永久小于块内实际字数
    （实测「思考 482 字」而块内 565 字）。
    """
    _boot(page, url)
    _emit(page, {"type": "user_message", "content": "q"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "thinking_content", "token": "一二三"})
    page.wait_for_timeout(160)
    s1 = _think_status(page)
    _check("思考期：状态行显示正在思考 + 字数", "思考中" in s1 and "3 字" in s1, s1)

    _emit(page, {"type": "thinking_content", "token": "四五六七"})
    page.wait_for_timeout(160)
    s2 = _think_status(page)
    _check("思考期：字数随 token 递增", "7 字" in s2, s2)

    # 模型停顿（无新 token）时时长仍在走
    page.wait_for_timeout(500)
    s3 = _think_status(page)
    _check(
        "思考期：无新 token 时长仍走表",
        _status_secs(s3) > _status_secs(s2) + 0.3,
        f"{_status_secs(s2)} -> {_status_secs(s3)}",
    )

    # 正文到达 → 折叠，字数定格为全文长度
    _emit(page, {"type": "chunk", "content": "答案"})
    page.wait_for_timeout(200)
    s4 = _think_status(page)
    _check("折叠后：不再显示「思考中」且字数正确", "思考中" not in s4 and "7 字" in s4, s4)
    secs_fold = _status_secs(s4)
    page.wait_for_timeout(450)
    again = _think_status(page)
    _check("折叠后：时长定格不再增长", _status_secs(again) == secs_fold, f"{secs_fold} -> {again}")

    # 迟到 token 并入正文块 → 状态行字数必须同步（旧实现会永久停在 7 字）
    _emit(page, {"type": "thinking_content", "token": "八九"})
    page.wait_for_timeout(200)
    lt = _last_think(page)
    ls = _think_status(page)
    _check("迟到 token：块内与状态行字数一致", lt["chars"] == 9 and "9 字" in ls, f"{lt} / {ls}")


def case_pure_think_tool_call_folds(page, url: str) -> None:
    """纯思考 + 工具调用（无正文帧）的卡片，思考栏也必须自动折叠。

    折叠原先只由「正文 chunk 到达」触发，这类轮次一个正文帧都没有 → 思考栏一直
    展开挂着。现在改为「轮次结束」（llm_end / tool_select …）兜底折叠。
    """
    _boot(page, url)
    _emit(page, {"type": "user_message", "content": "查一下目录"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "thinking_content", "token": "我需要调用工具"})  # 7 字
    page.wait_for_timeout(160)
    ht = _last_think(page)
    _check("工具轮：思考期展开", ht["state"] == "e" and ht["h"] > 0, str(ht))

    _emit(page, {"type": "llm_end", "content": "", "has_tool_calls": True})
    _emit(page, {"type": "tool_select", "tools": ["bash"]})
    _emit(page, {"type": "tool_call", "name": "bash", "args": '{"command":"ls"}', "tool_call_id": "t1"})
    page.wait_for_timeout(240)
    ct = _last_think(page)
    cs = _think_status(page)
    _check("纯思考+工具调用：思考栏自动折叠", ct["state"] == "c", str(ct))
    _check("折叠后正文收起（单行摘要）", ct["h"] == 0, str(ct))
    _check("折叠摘要含字数与时长", "思考 7 字" in cs and "s" in cs, cs)

    # 折叠只是收起来，全文必须还在：点一下能重新展开
    page.evaluate(
        "() => { var t = document.querySelectorAll('.assistant-msg .think-msg'); if (t.length) t[0].click(); }"
    )
    page.wait_for_timeout(220)
    et = _last_think(page)
    _check("折叠后全文仍可展开", et["state"] == "e" and et["h"] > 0 and et["chars"] == 7, str(et))


def case_empty_deltas_no_fake_think(page, url: str) -> None:
    """空 delta 不得伪造思考块、不得提前折叠思考栏。

    provider 会在流首发 `reasoning_content=""`、在思考中途发 `content=""`。
    照单全收的后果：无思考的轮次凭空长出「思考 0 字」空块；有思考的轮次在第一
    个空正文帧就被折叠（用户看到的是「思考栏没展开/提前收起」）。
    """
    _boot(page, url)
    # 轮1：两个空帧 → 不建块、不折叠
    _emit(page, {"type": "user_message", "content": "q1"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "thinking_content", "token": ""})
    _emit(page, {"type": "chunk", "content": ""})
    page.wait_for_timeout(160)
    st = _think_state(page)
    _check("空帧不建思考块", all(c["state"] == "-" for c in st), str(st))
    _emit(page, {"type": "llm_end", "content": "答案1"})
    _emit(page, {"type": "done", "session_id": "sess0001", "final_content": "答案1", "non_system_count": 2})
    page.wait_for_timeout(180)

    # 轮2：真思考 → 展开；空 chunk → 保持展开；真 chunk → 折叠
    _emit(page, {"type": "user_message", "content": "q2"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "thinking_content", "token": "真思考"})
    page.wait_for_timeout(160)
    last = _think_state(page)[-1]
    _check("真思考帧建块且展开", last["state"] == "e" and last["chars"] == 3 and last["h"] > 0, str(last))

    _emit(page, {"type": "chunk", "content": ""})
    page.wait_for_timeout(160)
    last = _think_state(page)[-1]
    _check("空正文帧不折叠思考栏", last["state"] == "e", str(last))

    _emit(page, {"type": "chunk", "content": "答案2"})
    page.wait_for_timeout(200)
    last = _think_state(page)[-1]
    _check("真正文帧折叠思考栏", last["state"] == "c", str(last))


def case_history_changed_midturn(page, url: str) -> None:
    """轮次中收到 history_changed：不得立刻 clearUI 打断流，等 done 后统一重拉。

    后端在**工具轮中途**也可能广播 history_changed（工具改写了上下文，如
    shortcircuit 退化历史块）。此刻前端正在流式渲染当前卡片：立刻 clearUI 会
    把卡片抹掉，而落盘历史尚不含本轮，重拉必失配。故前端只记账
    （`_pendingHistoryRefresh`），待 done/cancelled/error 收尾后统一刷。
    """
    _boot(page, url)
    # 覆盖 /messages 桩：返回可辨识的历史，用于确认「done 后确实重拉了」
    page.evaluate(
        """() => {
      window.fetch = async (u) => {
        const s = String(u);
        let body = {};
        if (s.indexOf('/api/health') >= 0) body = { agent: 'e2e-model', session: 'sess0001' };
        else if (s.indexOf('/messages') >= 0) body = { messages: [
          { role: 'assistant', content: '重写后的历史正文', index: 1 }] };
        else if (s.indexOf('/api/sessions') >= 0) body = { sessions: [] };
        else if (s.indexOf('/api/commands') >= 0) body = { commands: [] };
        return new Response(JSON.stringify(body), { status: 200,
          headers: { 'Content-Type': 'application/json' } });
      };
    }"""
    )
    # 注入事件绕过了真实提交路径 → 用 connected 的运行时快照恢复「处理中」状态
    # （否则前端 processing=false，闸门不生效，等于没测到这个分支）
    _emit(
        page,
        {
            "type": "connected",
            "run_id": "r1",
            "seq": 0,
            "session_id": "sess0001",
            "processing": True,
            "replay": False,
            "resync": False,
        },
    )
    _emit(page, {"type": "user_message", "content": "跑个会改上下文的工具"})
    _emit(page, {"type": "llm_start"})
    _emit(page, {"type": "chunk", "content": "半截正文正在流"})
    page.wait_for_timeout(220)  # 100ms 正文节流 flush
    _emit(page, {"type": "history_changed", "msg_count": 3})
    page.wait_for_timeout(260)  # 若立刻刷新，clearUI + fetch 会在 100ms 内抹掉卡片
    _check("轮次中收到 history_changed 不打断流（卡片仍在）", "半截正文正在流" in _text(page))

    _emit(
        page,
        {"type": "done", "session_id": "sess0001", "final_content": "半截正文正在流", "non_system_count": 3},
    )
    page.wait_for_timeout(320)  # done 侧 setTimeout(100) 后重拉
    txt = _text(page)
    _check("done 后消费挂起刷新：重拉历史替换失配卡片", "重写后的历史正文" in txt and "半截正文正在流" not in txt)


CASES = (
    case_command_after_text,
    case_normal_turn_no_dup,
    case_tool_turn,
    case_cancel_then_command,
    case_two_commands,
    case_resync_midturn,
    case_empty_deltas_no_fake_think,
    case_think_status_live,
    case_pure_think_tool_call_folds,
    case_history_changed_midturn,
)


def main() -> int:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("需要 playwright：pip install playwright && playwright install chromium")
        return 2

    srv, port = _serve()
    url = f"http://127.0.0.1:{port}/static/index.html"
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context()
            for pat in ("**cdn.jsdelivr.net/**", "**fonts.googleapis.com/**", "**fonts.gstatic.com/**"):
                ctx.route(pat, lambda r: r.abort())  # CDN 依赖与断言无关，离线跑
            ctx.add_init_script(INIT_JS)
            page = ctx.new_page()
            page.on("pageerror", lambda e: print("[pageerror]", str(e)[:200]))
            for case in CASES:
                print(f"\n=== {case.__name__} ===")
                case(page, url)
            browser.close()
    finally:
        srv.shutdown()

    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{'=' * 46}\n{len(RESULTS) - len(bad)}/{len(RESULTS)} 通过")
    for name, _, detail in bad:
        print("  FAIL:", name, detail)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
