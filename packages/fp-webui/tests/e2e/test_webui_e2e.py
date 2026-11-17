"""FP WebUI Playwright e2e 安全网（任务 #210 n3）——后续前端 ESM 重构的保护网。

设计原则：只断言**用户可见 DOM** 与公开 HTTP 契约，不断言内部函数名/变量名；
事件经 window.__fp_dispatch 接缝注入真实分发逻辑，不模拟 LLM。
选择器契约：#messagesContainer / #authOverlay / #inputField / #connectionStatus /
.tool-row(.tool-row-head/.tool-row-body, .collapsed) / #askCard/.ask-option。
"""

from __future__ import annotations

import json
import re
import time
import urllib.request

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# 已知问题豁免：登录后 connected 触发 GET /api/sessions/<sid>/messages，
# 当前 session 无消息文件 → 服务端 404（浏览器网络层必打 console error，
# 前端 fetchSessionHistory 已 catch 不受影响，但无法抑制该日志）。详见交付报告。
_HISTORY_404 = re.compile(r"/api/sessions/[^/]+/messages$")
# /api/build 是"本页是不是老模块"的探测端点，按设计**允许失败**（服务端进程
# 可能跑着没有该端点的旧 Python → 401；此时前端退回资产版本串自检）。
# 浏览器网络层对 401 必打一条 console error，前端 catch 得了但日志抑制不了。
_BUILD_PROBE_401 = re.compile(r"/api/build$")


def _ext_urls(errlog, base_url):
    """外部域（CDN/字体）失败 URL —— 网络不通的证据。"""
    out = [u for u in errlog["failed"] if u.startswith("http") and not u.startswith(base_url)]
    out += [
        u
        for t, u in errlog["console"]
        if "Failed to load resource" in t and u.startswith("http") and not u.startswith(base_url)
    ]
    return sorted(set(out))


def _js_problems(errlog, base_url):
    """真正的 JS 自身问题：pageerror + 非资源类 console error + 同源未豁免的资源错误。"""
    out = list(errlog["pageerror"])
    for text, url in errlog["console"]:
        if "Failed to load resource" in text:
            if url.startswith(base_url) and not (_HISTORY_404.search(url) or _BUILD_PROBE_401.search(url)):
                out.append(f"同源资源加载失败: {url}")
        else:
            out.append(text)
    return out


def _dispatch(page, event):
    page.evaluate("e => window.__fp_dispatch(e)", event)


# ── 1. 页面加载干净 ────────────────────────────────────────
def test_01_page_loads_clean(server, page, errlog):
    # 登录后主界面可见（fixture 已完成 goto + 登录）
    expect(page.locator("#authOverlay")).to_be_hidden()
    expect(page.locator("#messagesContainer")).to_be_visible()
    expect(page.locator("#inputField")).to_be_visible()
    expect(page.locator("#connectionStatus")).to_contain_text("已连接")

    ext = _ext_urls(errlog, server.base_url)
    if ext:  # CDN/字体拉不下来 = 网络不通 → skip 而非 fail（本地脚本有 typeof marked 守卫）
        pytest.skip(f"CDN/外部资源不可达（网络不通），无法验证加载干净: {ext[:3]}")
    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"页面加载存在错误: {problems}"


# ── 2. 契约端点 ────────────────────────────────────────────
def test_02_ui_schema_contract(server):
    req = urllib.request.Request(
        server.base_url + "/api/ui/schema", headers={"Authorization": "Bearer " + server.token}
    )
    with urllib.request.urlopen(req, timeout=10) as resp:
        assert resp.status == 200
        data = json.load(resp)
    assert "version" in data, "契约缺 version"
    assert "events" in data, "契约缺 events"
    assert len(data["events"]) == 31, f"WS 事件应为 31 个，实际 {len(data['events'])}"


# ── 3. 事件分发不吞卡（回归守卫）────────────────────────
def test_03_streaming_content_not_swallowed(page):
    content = "E2E_吞卡守卫_CONTENT_9f3a 前缀 中缀 后缀"
    _dispatch(page, {"type": "llm_start"})
    _dispatch(page, {"type": "chunk", "content": content[:18]})
    _dispatch(page, {"type": "chunk", "content": content[18:]})
    _dispatch(page, {"type": "llm_end", "content": content})
    _dispatch(page, {"type": "done"})
    # 守卫：消息容器里必须出现包含该 content 的文本（被吞=渲染丢失）
    expect(page.locator("#messagesContainer")).to_be_visible()
    expect(page.locator("#messagesContainer")).to_contain_text(content)


# ── 4. warning 可见（新修复回归）─────────────────────────
def test_04_warning_visible(page, errlog):
    mark = "E2E_WARNING_MARK_b72f 插件抑制提示"
    _dispatch(page, {"type": "warning", "content": mark})
    expect(page.locator("#messagesContainer")).to_contain_text(mark)
    assert not _js_problems(errlog, page.url.rsplit("/", 1)[0]), errlog


# ── 5. info 可见 ──────────────────────────────────────────
def test_05_info_visible(page, errlog):
    mark = "E2E_INFO_MARK_c41d 提示文本"
    _dispatch(page, {"type": "info", "content": mark})
    expect(page.locator("#messagesContainer")).to_contain_text(mark)
    assert not _js_problems(errlog, page.url.rsplit("/", 1)[0]), errlog


# ── 6. 未知事件类型不炸 ───────────────────────────────────
def test_06_unknown_event_type_does_not_break(page, errlog):
    _dispatch(page, {"type": "完全不存在的类型_xyz", "content": "x"})
    page.wait_for_timeout(300)
    # 页面仍可交互：输入框可输入
    page.fill("#inputField", "e2e交互探针")
    assert page.input_value("#inputField") == "e2e交互探针"
    page.fill("#inputField", "")
    problems = [p for p in _js_problems(errlog, page.url.rsplit("/", 1)[0])]
    assert not problems, f"未知事件类型引发 JS 错误: {problems}"


# ── 7. 折叠/展开不截断 ────────────────────────────────────
def test_07_tool_row_collapse_expand_not_truncated(page):
    head, tail = "E2E_TOOL_HEAD_a1", "E2E_TOOL_TAIL_a2"
    result = head + " " + ("中间内容段落 " * 30) + tail
    _dispatch(page, {"type": "tool_call", "name": "e2e_probe_tool", "args": {"k": "v"}, "tool_call_id": "e2e_tc_1"})
    _dispatch(page, {"type": "tool_result", "name": "e2e_probe_tool", "result": result, "tool_call_id": "e2e_tc_1"})
    row = page.locator(".tool-row").last
    expect(row).to_have_class(re.compile(r"\bcollapsed\b"))  # 完成即折叠
    body = row.locator(".tool-row-body")
    expect(body).not_to_be_visible()
    row.locator(".tool-row-head").click()
    expect(body).to_be_visible()
    txt = body.inner_text()
    assert head in txt and tail in txt, f"展开后文本被截断: {txt[:120]}..."
    row.locator(".tool-row-head").click()  # 再折叠
    expect(body).not_to_be_visible()
    row.locator(".tool-row-head").click()  # 再展开
    expect(body).to_be_visible()
    txt2 = body.inner_text()
    assert head in txt2 and tail in txt2, "折叠再展开后内容丢失"


# ── 8. 重连 replay/resync ─────────────────────────────────
def test_08_ws_reconnect_survives(page, errlog, server):
    expect(page.locator("#connectionStatus")).to_contain_text("已连接")
    page.evaluate("window.__fp_test.closeSocket()")  # 真实关闭 socket（ws 已收敛为模块局部，经测试接缝访问）
    expect(page.locator("#connectionStatus")).to_contain_text("已断开", timeout=4000)
    # onclose → 3s 自动重连 → 服务端 connected（replay/resync 由服务端裁决）
    expect(page.locator("#connectionStatus")).to_contain_text("已连接", timeout=12000)
    page.wait_for_timeout(600)  # 覆盖 resync 清屏 + 历史回拉
    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"重连过程出现 JS 错误: {problems}"
    # 页面未白屏：主界面骨架仍在
    expect(page.locator("#messagesContainer")).to_be_visible()
    expect(page.locator("#inputField")).to_be_visible()
    assert len(page.locator("body").inner_text().strip()) > 50, "页面疑似白屏"


# ── 9. ask 卡片恢复（bug#1 守卫）─────────────────────────
def test_09_ask_card_restored_on_resync(page, errlog):
    ask = {"type": "ask", "ask_id": "t1", "prompt": "选一个", "options": ["A", "B"]}
    _dispatch(page, ask)
    expect(page.locator("#askCard")).to_be_visible()
    expect(page.locator("#askCard .ask-option")).to_have_text(["A", "B"])
    # 断线重连：resync 清屏后，connected 携带 pending_ask **对象**必须恢复整卡
    _dispatch(
        page,
        {
            "type": "connected",
            "seq": 0,
            "run_id": "e2e_probe",
            "resync": True,
            "pending_ask": {"ask_id": "t1", "prompt": "选一个", "options": ["A", "B"]},
        },
    )
    expect(page.locator("#askCard")).to_be_visible(timeout=6000)
    expect(page.locator("#askCard .ask-option")).to_have_text(["A", "B"])
    expect(page.locator("#messagesContainer")).to_contain_text("选一个")
    assert not errlog["pageerror"], f"恢复过程 JS 报错: {errlog['pageerror']}"


# ── 10. 非 resync 的 connected+pending_ask 不重复建卡（n5 Store 幂等守卫）──
# n3 实测 bug：showAskCard 无条件 addAssistantMessage，且非 resync 不清屏，
# 第二次收到同一 pending_ask 会叠出第二张卡（DOM id 重复、选项渲染两遍、
# liveMsgIndex 被推高两位）。n5 改为状态驱动幂等渲染：同状态只画一次。
def test_10_pending_ask_idempotent_no_duplicate(page, errlog):
    ask = {"type": "ask", "ask_id": "t1", "prompt": "选一个", "options": ["A", "B"]}
    _dispatch(page, ask)
    expect(page.locator("#askCard")).to_be_visible()
    n_first = page.locator("#askCard").count()
    assert n_first == 1, f"首张 ask 卡应恰好 1 张，实为 {n_first}"

    # 非 resync 重连（不清屏）：同一 pending_ask 再次到达 → 不得叠卡
    for _ in range(3):
        _dispatch(
            page,
            {
                "type": "connected",
                "seq": 0,
                "run_id": "e2e_probe",
                "resync": False,
                "pending_ask": {"ask_id": "t1", "prompt": "选一个", "options": ["A", "B"]},
            },
        )
    page.wait_for_timeout(400)
    n = page.locator("#askCard").count()
    assert n == 1, f"重复 pending_ask 后应仍为 1 张卡（幂等），实为 {n} 张"
    # 选项按钮也只得渲染一遍
    n_btn = page.locator("#askCard .ask-option").count()
    assert n_btn == 2, f"选项应恰好 2 个，实为 {n_btn}"

    # 答复后卡片应撤销（状态置 null → 渲染函数撤卡）
    _dispatch(page, {"type": "done", "text": "ok"})
    page.wait_for_timeout(200)
    assert page.locator("#askCard").count() == 0, "答复后 ask 卡应被撤销"
    assert not errlog["pageerror"], f"幂等渲染出现 JS 报错: {errlog['pageerror']}"


# ── 11. 渲染注册表：自举能力（n6）──────────────────────────
# 核心验收：**不改 ui.js 源码**即可定制工具卡渲染。
# 旧实现是硬编码模板，新增定制必须动 ui.js —— 那段 JS 无测试保护，
# 正是自举的瓶颈。注册表让定制渲染退化为一次 registerRenderer 调用。
def test_11_renderer_registry_self_bootstrap(page, errlog):
    # ① 未注册的未知工具 → 走通用兜底（绝不丢弃，对齐 TUI 扩展开口）
    _dispatch(
        page, {"type": "tool_call", "name": "never_registered_tool", "args": {"x": 1}, "tool_call_id": "e2e_unreg_1"}
    )
    _dispatch(
        page,
        {
            "type": "tool_result",
            "name": "never_registered_tool",
            "result": "unreg-result",
            "tool_call_id": "e2e_unreg_1",
        },
    )
    unreg = page.locator('.tool-row[data-tool-call-id="e2e_unreg_1"]')
    expect(unreg).to_be_visible()
    ubody = unreg.locator(".tool-row-body")
    ubody_txt = ubody.inner_text() if ubody.count() else ""
    # 兜底渲染：参数 JSON 与返回值都必须在（含折叠前的 DOM 文本）
    assert "unreg-result" in ubody_txt or "返回" in ubody_txt, f"未知工具未走通用兜底: {ubody_txt[:150]}"

    # ② 运行时注册定制渲染器（等价于"新增工具卡"，全程不碰 ui.js）
    page.evaluate("""() => {
        window.__fp_test.registerRenderer('e2e_custom_tool', {
            args: (ctx) => '<div class="e2e-custom-args">ARG:' + ctx.name + ':' +
                            JSON.stringify(ctx.args) + '</div>',
            result: (ctx) => '<div class="e2e-custom-result">RES:' + ctx.result + '</div>'
        });
    }""")
    registered = page.evaluate("() => window.__fp_test.registeredTools()")
    assert "e2e_custom_tool" in registered, f"注册表未记录该工具: {registered}"

    # ③ 注册后新增工具 → 定制渲染生效（未改任何前端源码）
    _dispatch(
        page,
        {"type": "tool_call", "name": "e2e_custom_tool", "args": {"path": "/tmp/x"}, "tool_call_id": "e2e_tc_custom"},
    )
    _dispatch(
        page, {"type": "tool_result", "name": "e2e_custom_tool", "result": "custom-ok", "tool_call_id": "e2e_tc_custom"}
    )
    crow = page.locator('.tool-row[data-tool-call-id="e2e_tc_custom"]')
    expect(crow).to_be_visible()
    expect(crow.locator(".e2e-custom-args")).to_contain_text("ARG:e2e_custom_tool")
    expect(crow.locator(".e2e-custom-result")).to_contain_text("RES:custom-ok")

    # ④ 定制渲染不得破坏状态机：完成态仍要折叠、状态图标仍要更新
    expect(crow).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(crow.locator(".tool-state-icon")).to_have_text("✅")
    expect(crow.locator(".tool-row-status")).to_contain_text("完成")

    assert not errlog["pageerror"], f"注册表渲染出现 JS 报错: {errlog['pageerror']}"


def test_12_backend_driven_tool_meta(page, errlog):
    """n7 验收：后端元信息驱动参数区渲染，前端源码零改动。

    与 test_11 的区别：test_11 证明「手写渲染器」可运行时注册；
    本条证明「无需手写任何渲染器」——只要后端下发元信息，前端
    自动把裸 JSON 升级为结构化参数视图。这正是自举的落点：
    新增工具的卡片展示由插件/后端决定，前端不参与。
    """
    # ① 登录后元信息已加载（initApp 时机正确，无 401）
    #    必须**等**而不是瞬时读：登录遮罩消失只代表 WS 连通，而
    #    /api/ui/schema 是 initApp 里 fire-and-forget 的异步请求，两者
    #    无先后保证。瞬时断言在高负载下会翻车（实测 e2e 单独连跑时偶发）。
    #    用 wait_for_function（Python 版 expect 无 poll）。
    try:
        page.wait_for_function("() => window.__fp_test.toolMetaLoaded()", timeout=15000)
    except Exception:
        raise AssertionError("工具卡元信息未加载（initApp 时机 / 鉴权问题）") from None
    count = page.evaluate("() => window.__fp_test.toolCardCount()")
    assert count > 0, "后端未下发任何工具卡元信息"

    # ② 从**已缓存**的元信息里取第一个带参数的工具 —— **不硬编码
    #    工具名**，后端加/减工具测试自动适配（这才叫数据驱动）。
    #    走缓存而非重新 fetch：测试期间 token 可能已被 401 分支的
    #    clearToken 清除，重复发请求会撞 401（实测踩过）。
    target = page.evaluate("""() => {
        const T = window.__fp_test;
        for (const n of T.toolCardNames()) {
            const c = T.getToolCard(n);
            if (c && Array.isArray(c.params) && c.params.length > 0) {
                return {name: c.name, params: c.params.map(p => p.name)};
            }
        }
        return null;
    }""")
    assert target, "缓存里没有任何带参数的工具（后端 definitions 为空？）"
    tool = target["name"]

    # ③ 注入该工具的 tool_call → 参数区应为结构化视图而非裸 JSON
    args = {p: "v_" + p for p in target["params"][:2]}
    _dispatch(page, {"type": "tool_call", "name": tool, "args": args, "tool_call_id": "e2e_meta_1"})
    row = page.locator('.tool-row[data-tool-call-id="e2e_meta_1"]')
    expect(row).to_be_visible()

    meta = row.locator(".tool-args-meta")
    expect(meta).to_be_visible()
    # 结构化行里的参数名来自后端元信息（前端不认识这个工具）
    first_name = row.locator(".tool-args-meta-name").first
    expect(first_name).to_contain_text(target["params"][0])

    # 实际传入的值必须在（元信息只有 schema，没有值 → 两者须合并）
    expect(row.locator(".tool-args-value").first).to_contain_text("v_" + target["params"][0])

    # 原始 JSON 折叠保留：结构化视图不丢字节
    expect(row.locator(".tool-args-raw")).to_be_visible()

    # ④ 不得破坏状态机：完成后仍折叠、状态图标仍更新
    _dispatch(page, {"type": "tool_result", "name": tool, "result": "meta-ok", "tool_call_id": "e2e_meta_1"})
    expect(row).to_have_class(re.compile(r"\bcollapsed\b"))
    expect(row.locator(".tool-state-icon")).to_have_text("✅")

    assert not errlog["pageerror"], f"元信息渲染出现 JS 报错: {errlog['pageerror']}"


# ── 12. #228 单连接不变式：CLOSING 窗口不得开第二条、不得谎报「已断开」 ──
def test_13_no_parallel_socket_when_reconnecting(page, errlog, server):
    """历史 bug（用户实测：ws 连上 → 立刻断开 → 又连上）：

    visibilitychange 处理器在原实现里只挡 OPEN/CONNECTING，看到 CLOSING 就
    直接再开一条；而正在退场的那条 socket 的 onclose 随后仍把状态写成
    「🔴 已断开」并排一条 3s 重连——用户于是看到"两次加载才连上"。

    修复后：CLOSING 只挂号（immediateReconnect），旧连接一退场 0ms 接上，
    全程无并行连接、无「已断开」误报。
    """
    expect(page.locator("#connectionStatus")).to_contain_text("已连接")
    before = page.evaluate("window.__fp_test.wsStats()")
    assert before["created"] == 1, f"登录后应只建连 1 次，实际 {before}"

    # 记录状态文本的全部变化（用户能看到的那一句）
    page.evaluate(
        """() => {
            window.__statusLog = [];
            const el = document.getElementById('connectionStatus');
            new MutationObserver(() => {
                const t = (el.textContent || '').trim();
                if (window.__statusLog[window.__statusLog.length - 1] !== t) window.__statusLog.push(t);
            }).observe(el, {childList: true, characterData: true, subtree: true});
        }"""
    )

    # 关键窗口：close() 之后、close 事件到达之前派发 visibilitychange
    page.evaluate(
        """() => {
            Object.defineProperty(document, 'visibilityState', {value: 'visible', configurable: true});
            window.__fp_test.closeSocket();
            document.dispatchEvent(new Event('visibilitychange'));
        }"""
    )
    expect(page.locator("#connectionStatus")).to_contain_text("已连接", timeout=12000)
    page.wait_for_timeout(1200)  # 覆盖原实现那条 3s 定时器的窗口

    after = page.evaluate("window.__fp_test.wsStats()")
    log = page.evaluate("window.__statusLog")
    assert after["created"] == before["created"] + 1, (
        f"重连应恰好新增 1 条连接（并行建连=双连 bug）: {before} → {after}"
    )
    assert not any("已断开" in t for t in log), f"重连路径不得出现「已断开」误报（应走 immediate 的「重连中」）: {log}"

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"重连过程出现 JS 错误: {problems}"


# ── 14. 同一等待场景只允许一处 loading 提示（#227 目标②）──────
def test_14_single_loading_hint_on_disconnect(page, errlog, server):
    """用户实测吐槽："等待会话加载的时候，甚至有旧版和新版两种提示，非常丑"。

    断联重连路径曾经同时挂两处：状态栏行内「3 秒后重连…」spinner +
    页面级全屏遮罩「与服务器连接已断开，正在重连…」。用户看到的是两层
    loading 叠加（且遮罩盖住正在读的内容）。修复后只保留行内一处。
    """
    expect(page.locator("#connectionStatus")).to_contain_text("已连接")

    # 以 100ms 采样，记录「行内 spinner」与「页面级遮罩」是否同帧并存
    page.evaluate(
        """() => {
            window.__hintLog = [];
            const sample = () => {
                const st = document.getElementById('connectionStatus');
                const inline = !!(st && st.querySelector('.fp-spin-box'));
                // #fpPageLoading 是 position:fixed —— offsetParent 恒为 null，
                // 不能用它判可见性；显隐机制是 body 上的 fp-page-done 类 + 淡出后被移除
                const ov = document.getElementById('fpPageLoading');
                const ovVis = !!(ov && !ov.classList.contains('fp-page-done')
                                 && getComputedStyle(ov).display !== 'none'
                                 && getComputedStyle(ov).visibility !== 'hidden');
                window.__hintLog.push({t: Date.now(), inline, ovVis});
            };
            window.__hintTimer = setInterval(sample, 100);
            sample();
        }"""
    )
    page.evaluate("window.__fp_test.closeSocket()")  # 走 3s 定时重连路径（非 immediate）
    expect(page.locator("#connectionStatus")).to_contain_text("已连接", timeout=15000)
    page.wait_for_timeout(400)
    page.evaluate("clearInterval(window.__hintTimer)")
    log = page.evaluate("window.__hintLog")

    assert any(s["inline"] for s in log), f"断联期间行内应有 spinner（采样 {len(log)} 帧）"
    both = [s for s in log if s["inline"] and s["ovVis"]]
    assert not both, f"行内 spinner 与页面级遮罩不得同帧并存（双提示）: {len(both)} 帧命中"

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"重连过程出现 JS 错误: {problems}"


# ── 重连历史回归（用户实测：重连 + agent 忙 → 历史消失 / 编号从 #1 重来 / 顺序倒置）──
_HIST3 = {
    "session_id": "sess-e2e",
    "total": 3,
    "non_system_count": 3,
    "messages": [
        {"role": "user", "content": "历史问题一", "index": 1},
        {"role": "assistant", "content": "历史回答一", "index": 2},
        {"role": "user", "content": "历史问题二", "index": 3},
    ],
}


def _mock_history(page):
    page.route(
        "**/api/sessions/*/messages",
        lambda r: r.fulfill(status=200, content_type="application/json", body=json.dumps(_HIST3)),
    )


def _container_order(page):
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('#messagesContainer > *')).map(el => {
            if (el.classList.contains('history-separator')) return 'HISTORY';
            if (el.querySelector('.user-msg')) return 'user';
            if (el.querySelector('.assistant-msg')) return 'asst';
            return 'other';
        })"""
    )


def _index_badges(page):
    return page.evaluate("() => Array.from(document.querySelectorAll('.msg-index-badge')).map(e => e.textContent)")


def _wait_history_settled(page, timeout=8000):
    """等登录瞬间的历史 REST 落地（容器子节点数连续两次采样稳定即算落定）。

    为什么需要：本夹具复用真服务端，`page` 登录完成 ≠ 历史渲染完成。此后任何
    「先亲手清空 DOM、再断言容器为空/无新节点」的用例都会被在途历史打成假阴/假阳
    （test_18 实测抖过一次）。宁可多等，也别让异步帧冒充被测行为。
    """
    deadline = time.time() + timeout / 1000.0
    last, stable = None, 0
    while time.time() < deadline:
        n = page.evaluate("() => document.querySelectorAll('#messagesContainer > *').length")
        stable = stable + 1 if n == last else 0
        if stable >= 2:
            return n
        last = n
        page.wait_for_timeout(200)
    return last


def test_15_reconnect_while_busy_keeps_history_and_index(page, errlog, server):
    """用户实测：重连后若 agent 正忙 → ①历史不显示、实时消息从 #1 重新编号；
    ②期间切会话，历史出现但新消息长在「📜 历史记录」横幅上方（顺序倒置）。

    根因：connected 的重拉条件写的是 `!data.processing`。resync（服务端重启/
    缓冲溢出）已经 clearUI 把视图清空，却因"正在跑"跳过重拉 → 空白 + 编号基准丢失
    （liveMsgIndex 归 0 后再没人校准，实时消息只能从 #1 数起，回溯索引整体错位）。
    历史还有第二处隐患：它是 `container.appendChild` 追加的，只要 REST 返回前有
    任何节点进来（实时卡片/切会话提示），历史就落到它们后面。
    """
    _mock_history(page)
    # 一次非 resync 的"断线重连"：清屏 + 全量重拉，把 mock 历史拉进来
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-1",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": True,
            "resync": False,
            "processing": False,
            "pending_ask": None,
        },
    )
    expect(page.locator(".history-separator")).to_be_visible(timeout=10000)
    assert _container_order(page) == ["HISTORY", "user", "asst", "user"]
    assert _index_badges(page) == ["#1", "#2", "#3"]

    # 重连 + agent 忙 + resync：历史必须回来（而不是留一片空白），编号必须接着数
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-2",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": False,
            "resync": True,
            "processing": True,
            "pending_ask": None,
        },
    )
    expect(page.locator(".history-separator")).to_be_visible(timeout=10000)
    page.wait_for_timeout(300)
    assert _container_order(page)[0] == "HISTORY", _container_order(page)
    assert _index_badges(page) == ["#1", "#2", "#3"], "重连后历史必须还在"
    # 重连相关的横幅一条都不许留（用户指出两次）：连接态/任务态都归状态栏。
    # 判据取「容器里既没有系统的居中提示行，也不含这些字样」——只放行 spinner 占位。
    body_text = page.evaluate(
        """() => Array.from(document.querySelectorAll('#messagesContainer > *'))
                 .filter(el => !el.hasAttribute('data-fp-loading'))
                 .map(el => el.innerText).join('\\n')"""
    )
    for banned in ("已重新连接", "会话已重新同步", "会话内容已重新同步", "继续运行中"):
        assert banned not in body_text, f"重连后又冒出横幅「{banned}」: {body_text!r}"

    # 实时流落在历史之后，且编号从 #4 续（历史 3 条之后的真实位置）
    _dispatch(page, {"type": "llm_start"})
    _dispatch(page, {"type": "chunk", "content": "重连后的实时回答"})
    _dispatch(page, {"type": "llm_end", "content": "重连后的实时回答"})
    page.wait_for_timeout(400)
    order = _container_order(page)
    assert order[0] == "HISTORY" and order[-1] == "asst", order
    assert _index_badges(page)[-1] == "#4", f"编号未与历史对齐: {_index_badges(page)}"

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"重连历史路径出现 JS 错误: {problems}"


def test_16_history_anchored_top_when_stream_wins_race(page, errlog, server):
    """历史是「更早的内容」，必须锚定容器最前——与 REST 何时返回无关。

    实测路径：agent 正忙 → 重连/切会话触发 clearUI + 异步重拉，而流式 chunk 在
    历史到达之前就建好了卡片。历史若 appendChild，就会落到实时卡片**后面**：
    用户看到「📜 历史记录」横幅下面全是旧消息、新消息反倒长在横幅上方，顺序全乱。
    """
    _mock_history(page)
    # 让 /messages 慢 2.5s 返回，制造"实时卡片先到、历史后到"的竞态。
    # 延迟必须远大于「一串 dispatch 的往返耗时」：否则历史可能在最后一个 evaluate
    # 返回前就落地，用例看着绿、竞态一次也没踩到（本用例最初写 400ms 就是假绿）。
    page.evaluate(
        """hist => {
            window.__realFetch = window.fetch;
            window.fetch = function (url, opts) {
                if (String(url).includes('/messages')) {
                    return new Promise(res => setTimeout(() => res(new Response(
                        JSON.stringify(hist),
                        {status: 200, headers: {'Content-Type': 'application/json'}})), 2500));
                }
                return window.__realFetch.apply(this, arguments);
            };
        }""",
        _HIST3,
    )
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-3",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": False,
            "resync": True,
            "processing": True,
            "pending_ask": None,
        },
    )
    # 历史还在路上：先让流式卡片落地
    _dispatch(page, {"type": "llm_start"})
    _dispatch(page, {"type": "chunk", "content": "历史到达前就开流"})
    expect(page.locator(".assistant-msg")).to_be_visible(timeout=5000)
    expect(page.locator(".history-separator")).to_be_visible(timeout=5000)

    order = _container_order(page)
    assert order[0] == "HISTORY", f"历史必须锚定最前，实际: {order}"
    assert order.index("asst") > 0, f"实时卡片必须在历史之后，实际: {order}"
    # 历史只应有一份（重渲染幂等，不叠加）
    assert order.count("HISTORY") == 1 and order.count("user") == 2, order

    page.evaluate("() => { if (window.__realFetch) window.fetch = window.__realFetch; }")
    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"竞态路径出现 JS 错误: {problems}"


def test_17_stale_baked_index_realigned_after_history(page, errlog, server):
    """历史后到 → 实时卡片/工具行**已烧进**的索引必须整体重排（徽章 + ↩ 两处副本）。

    索引在建卡时就写进 innerHTML（徽章文本 + backtrack-btn 的 data-index），而
    clearUI 已把 liveMsgIndex 归 0。「历史还在路上」期间建的卡片只能拿到 #1 起的
    临时号，历史落地后若不按文档序重排：
      · 徽章显示 #1、实际位置 #4 → 用户看到的正是「历史在、实时消息却从 #1 重来」；
      · 工具行的 ↩ 是第二份副本（dataset.msgIndex / data-index），只改徽章就是
        「徽章 #5、点 ↩ 却回到 #2」——回溯错位＝直接丢内容，比不显示更坏。
    """
    _mock_history(page)
    page.evaluate(
        """hist => {
            window.__realFetch = window.fetch;
            window.fetch = function (url, opts) {
                if (String(url).includes('/messages')) {
                    return new Promise(res => setTimeout(() => res(new Response(
                        JSON.stringify(hist),
                        {status: 200, headers: {'Content-Type': 'application/json'}})), 2500));
                }
                return window.__realFetch.apply(this, arguments);
            };
        }""",
        _HIST3,
    )
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-4",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": False,
            "resync": True,
            "processing": True,
            "pending_ask": None,
        },
    )
    # 历史在路上：先建满三张实时卡（助手卡 → 工具行 → 用户消息），全部拿临时号
    _dispatch(page, {"type": "llm_start"})
    _dispatch(page, {"type": "chunk", "content": "抢在历史之前的正文"})
    _dispatch(page, {"type": "llm_end", "content": "抢在历史之前的正文"})
    _dispatch(page, {"type": "tool_select", "tools": [{"name": "bash"}]})
    _dispatch(page, {"type": "tool_call", "name": "bash", "args": {"command": "ls"}, "tool_call_id": "tc-race"})
    _dispatch(page, {"type": "tool_result", "name": "bash", "result": "a.txt", "tool_call_id": "tc-race"})
    _dispatch(page, {"type": "done", "final_content": "抢在历史之前的正文", "session_id": "sess-e2e"})
    _dispatch(page, {"type": "user_message", "content": "抢在历史之前的追问"})
    page.wait_for_timeout(100)
    # 前提校验：历史尚未落地时，编号确实是从 #1 起（证明本用例真的走到了竞态）
    assert _index_badges(page) == ["#1", "#2", "#3"], _index_badges(page)

    expect(page.locator(".history-separator")).to_be_visible(timeout=10000)
    page.wait_for_timeout(300)

    # 历史 3 条 → 实时三卡续上 #4/#5/#6，工具行的 ↩ 副本同步到 5
    assert _index_badges(page) == ["#1", "#2", "#3", "#4", "#5", "#6"], _index_badges(page)
    assert page.evaluate("() => document.querySelector('.tool-row').dataset.msgIndex") == "5"
    assert page.evaluate("() => document.querySelector('.tool-row .backtrack-btn').dataset.index") == "5", (
        "工具行 ↩ 的 data-index 未随徽章重排 → 回溯会回到错误位置"
    )
    # 消息卡的 ↩ 同理：实时消息卡不得残留临时号
    live_bt = page.evaluate(
        """() => Array.from(document.querySelectorAll('.backtrack-btn'))
                 .filter(b => !b.closest('[data-fp-history]'))
                 .map(b => b.dataset.index)"""
    )
    assert live_bt == ["4", "5", "6"], live_bt

    page.evaluate("() => { if (window.__realFetch) window.fetch = window.__realFetch; }")
    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"编号重排路径出现 JS 错误: {problems}"


def test_18_reload_banner_not_duplicated(page, errlog, server):
    """重载期间消息流不许再挂横幅：状态栏已经在说同一件事。

    用户原话：「spinner 下方还有一个 emoji 开头的横幅，感觉重复了」。
    重载的三个事实——正在重建 / 已连接 / 已重载——状态栏 #connectionStatus
    各有对应文本，消息流里再各挂一条就是一问三答。本用例锁死这条口径：
    重载全过程容器内不得出现 ⏳/🔄 横幅，状态栏必须能自证。

    断言口径是「**没有新增横幅**」，而不是「容器为空」：本夹具复用真服务端，
    登录瞬间的历史 REST 可能还在路上，清空 DOM 后它会照常落地（那是正确行为）。
    旧写法 `innerHTML=''` + 断言空 → 被这拍异步历史打成假阴性/假阳性（实测抖过一次）。
    现改为：先等历史落定，再按「禁用词 + 容器子节点数不变」判定——
    横幅必然是**新增的节点**，正文里也不会自然长出「重建中/已重新连接」。
    """
    container = page.locator("#messagesContainer")
    _wait_history_settled(page)  # 先把在途历史等落地，别让它冒充横幅
    baseline_nodes = page.evaluate("() => document.querySelectorAll('#messagesContainer > *').length")
    banned = ("重建中", "重载完成", "已重新连接", "重新同步", "继续运行中")

    def _flow_banners():
        txt = container.inner_text()
        return [w for w in banned if w in txt]

    _dispatch(page, {"type": "reload"})
    page.wait_for_timeout(150)
    assert "重建中" in page.locator("#connectionStatus").inner_text()
    assert _flow_banners() == [], f"重载开始不该有横幅: {_flow_banners()}"
    assert page.evaluate("() => document.querySelectorAll('#messagesContainer > *').length") == baseline_nodes, (
        "重载开始不该往消息流里插节点"
    )

    _dispatch(page, {"type": "reload_done", "session_id": "sess-e2e", "model": "m"})
    page.wait_for_timeout(150)
    assert "已重载" in page.locator("#connectionStatus").inner_text(), (
        "状态栏必须自证「已重载」，否则删掉横幅后就真的没有任何重载反馈了"
    )
    assert _flow_banners() == [], f"重载完成不该有横幅: {_flow_banners()}"
    assert page.evaluate("() => document.querySelectorAll('#messagesContainer > *').length") == baseline_nodes, (
        "重载完成不该往消息流里插节点"
    )

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"重载路径出现 JS 错误: {problems}"


def test_19_stale_build_hint_appears_on_reconnect(page, errlog, server):
    """本页跑的是老模块时，「该刷新了」必须看得见（而不是让用户以为"没改好"）。

    由来（多轮实测）：前端是 ESM import 图，模块 URL 写死，改文件不改 URL；
    no-cache 也只在**重新导航**时生效——agent 热重启 / WS 重连 / 手机 bfcache
    回前台都不重新下载模块。于是"代码改了、界面还是老行为"被反复误判成
    "没改好"。修法：启动立基准戳，每次连上再取一次，戳变大就露可点刷新入口。

    本用例把 /api/build 抬高一档模拟"磁盘上的前端比本页新"，再走一次重连。
    """
    hint = page.locator("#buildHint")
    assert not hint.is_visible(), "本页不落后时不得出现刷新提示（否则就是新噪声）"

    real_stamp = page.evaluate("() => fetch('/api/build').then(r => r.json()).then(d => d.stamp)")
    page.route(
        "**/api/build",
        lambda r: r.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"stamp": real_stamp + 1}),
        ),
    )
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-b",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": True,
            "resync": False,
            "processing": False,
            "pending_ask": None,
        },
    )
    expect(hint).to_be_visible(timeout=5000)
    assert "前端已更新" in hint.inner_text()
    assert hint.is_enabled(), "刷新入口必须可点（唯一动作是重新导航）"

    # 文案口径：它只说"前端有新代码"，不得复述连接态（否则又是一条重复横幅）
    txt = hint.inner_text()
    for banned in ("已重新连接", "已同步", "已连接"):
        assert banned not in txt, f"刷新提示不该谈连接态: {txt!r}"

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"构建戳路径出现 JS 错误: {problems}"


def test_20_stale_hint_survives_old_server(page, errlog, server):
    """服务端进程跑着**旧 Python**（/api/build 不存在/401）时，过期提示仍须工作。

    这是实测里最常见的一格：用户改的是前端，服务端进程几天没重启 →
    /api/build 直接 401。若"我是不是老模块"只靠这一个端点，它就永远答不上来，
    页面继续安静地跑旧模块（正是"横幅早删了、用户说还在"的成因）。

    故前端另有一条**不依赖任何新端点**的信号：index.html 里的 `?_t=` 资产版本串。
    服务端（老代码也认）按 mtime 改写它，页面把"本页加载时那一套"和"磁盘上这一套"
    逐条比对即可。本用例把 /api/build 打成 401、再把 HTML 里一枚 `_t` 抬高模拟
    磁盘更新，重连后提示必须出现；反向（HTML 未变）则不得出现——不许误报。
    """
    hint = page.locator("#buildHint")
    real_html = page.evaluate("() => fetch('/', {cache: 'no-store'}).then(r => r.text())")

    # 老服务端：端点 401；HTML 未变 → 不许误报
    page.route(
        "**/api/build",
        lambda r: r.fulfill(
            status=401, content_type="application/json", body=json.dumps({"detail": "未授权，请先登录"})
        ),
    )
    page.route(re.compile(r"/$"), lambda r: r.fulfill(status=200, content_type="text/html", body=real_html))
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-c",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": True,
            "resync": False,
            "processing": False,
            "pending_ask": None,
        },
    )
    page.wait_for_timeout(500)
    assert not hint.is_visible(), "HTML 没变时不得报『前端已更新』（探测失败≠过期）"

    # 老服务端 + HTML 变了（磁盘更新）→ 必须报出来
    bumped = re.sub(r"\?_t=(\d+)", lambda m: "?_t=" + str(int(m.group(1)) + 1), real_html, count=1)
    assert bumped != real_html, "index.html 里没有可抬高的 ?_t= 版本串"
    page.unroute(re.compile(r"/$"))
    page.route(re.compile(r"/$"), lambda r: r.fulfill(status=200, content_type="text/html", body=bumped))
    _dispatch(
        page,
        {
            "type": "connected",
            "run_id": "e2e-run-d",
            "seq": 0,
            "session_id": "sess-e2e",
            "replay": True,
            "resync": False,
            "processing": False,
            "pending_ask": None,
        },
    )
    expect(hint).to_be_visible(timeout=5000)

    problems = _js_problems(errlog, server.base_url)
    assert not problems, f"资产版本自检路径出现 JS 错误: {problems}"
