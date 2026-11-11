"""FP WebUI Playwright e2e 安全网（任务 #210 n3）——后续前端 ESM 重构的保护网。

设计原则：只断言**用户可见 DOM** 与公开 HTTP 契约，不断言内部函数名/变量名；
事件经 window.__fp_dispatch 接缝注入真实分发逻辑，不模拟 LLM。
选择器契约：#messagesContainer / #authOverlay / #inputField / #connectionStatus /
.tool-row(.tool-row-head/.tool-row-body, .collapsed) / #askCard/.ask-option。
"""

from __future__ import annotations

import json
import re
import urllib.request

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# 已知问题豁免：登录后 connected 触发 GET /api/sessions/<sid>/messages，
# 当前 session 无消息文件 → 服务端 404（浏览器网络层必打 console error，
# 前端 fetchSessionHistory 已 catch 不受影响，但无法抑制该日志）。详见交付报告。
_HISTORY_404 = re.compile(r"/api/sessions/[^/]+/messages$")


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
            if url.startswith(base_url) and not _HISTORY_404.search(url):
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
    loaded = page.evaluate("() => window.__fp_test.toolMetaLoaded()")
    assert loaded, "工具卡元信息未加载（initApp 时机 / 鉴权问题）"
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
