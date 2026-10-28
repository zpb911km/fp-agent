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


CASES = (
    case_command_after_text,
    case_normal_turn_no_dup,
    case_tool_turn,
    case_cancel_then_command,
    case_two_commands,
    case_resync_midturn,
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
