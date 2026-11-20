"""e2e：指认式报障（core/diag + core/diagui）。

验的是三件互相独立、缺一不可的事：

1. **环在跑**：帧/交互/容器子节点数真的被记下来（没记 = 事后没证据）。
2. **长按真的冻结**，且**超时长按不会误提交**（回归：原按时间窗挡抬指，
   长按超过 1.75s 松手会把状态栏那一点当成指认提交上去）。
3. **指认 → filehub 文件**：点一下目标元素，json 落进 inbox 并自动挂进
   待发标记（用户按 ➤ 即发）。这断言的是用户侧的完整闭环，不是内部函数。

归属显示（顶部条实时写出「这一块属于哪个组件」）单独断言——那是"不知道
错在哪个部分"这条需求的正解，不能只靠事后读 json 反推。
"""

from __future__ import annotations

import json
import re
import urllib.request

import pytest
from playwright.sync_api import expect

pytestmark = pytest.mark.e2e

# 探针卡片：固定定位，避免消息区滚动导致坐标点空；class 与真实助手卡一致，
# 这样能顺带验证 COMP_MAP 的映射（归属必须是 components/messages.js）。
PROBE = """() => {
  const c = document.getElementById('messagesContainer');
  const d = document.createElement('div');
  d.id = 'diagProbe';
  d.className = 'msg assistant-msg';
  d.textContent = '探针卡片';
  Object.assign(d.style, { position: 'fixed', left: '16px', top: '140px',
                           width: '280px', height: '64px', zIndex: '60',
                           background: '#2b3138' });
  c.appendChild(d);
  const r = d.getBoundingClientRect();
  return { x: r.left + r.width / 2, y: r.top + r.height / 2 };
}"""


def _long_press(page, ms: int) -> None:
    """在状态栏上按住 ms 毫秒再松手——真机长按路径（同一 pointer 序列）。"""
    box = page.locator(".status-bar").bounding_box()
    assert box, "状态栏不在视口内，长按入口无法命中"
    page.mouse.move(box["x"] + 24, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(ms)
    page.mouse.up()


def _inbox_json(server, name: str) -> dict:
    url = f"{server.base_url}/api/files/download?box=inbox&name={name}"
    req = urllib.request.Request(url, headers={"Authorization": "Bearer " + server.token})
    with urllib.request.urlopen(req, timeout=10) as r:  # noqa: S310 —— 测试内固定地址
        return json.loads(r.read().decode())


def test_ring_is_recording(page):
    """环在登录后即开始记：没有证据就谈不上定位。"""
    page.wait_for_function("() => window.__fp_diag && window.__fp_diag.ring().length > 0")
    kinds = page.evaluate("() => window.__fp_diag.ring().map(e => e[1])")
    assert "I" in kinds  # init 记录，证明 install 走到末尾
    assert "C" in kinds  # 连接事件（open），证明 ws.js 的 connEvent 已接上
    assert kinds.count("W") > 0, f"WS 帧未被记录：{kinds[:20]}"


def test_long_press_freezes_without_false_commit(page):
    """长按 2.0s（越过 1.5s 阈值后继续按住）→ 冻结成立，且松手不算一次指认。"""
    page.wait_for_function("() => window.__fp_diag && window.__fp_diag.ring().length > 0")
    _long_press(page, 2000)

    expect(page.locator("html[data-diag-frozen='1']")).to_have_count(1)
    expect(page.locator(".fp-diag-mask")).to_have_count(1)
    # 误提交回归：这一抬指没有对应的按下（按下点在状态栏），不得产生待发文件。
    # 断言落到"有没有诊断条目"而非容器可见性——容器可见性先前被 CSS 的
    # .pending-files{display:flex} 污染过（[hidden] 被作者样式压掉），
    # 那种假阳性会把真正的误提交一起放过。
    page.wait_for_timeout(300)
    assert page.locator("#pendingFiles .pending-file").count() == 0, "超时长按松手被误当成指认提交了"

    # 退出后解冻，页面回到可交互
    page.keyboard.press("Escape")
    expect(page.locator("html[data-diag-frozen='1']")).to_have_count(0)
    expect(page.locator(".fp-diag-mask")).to_have_count(0)


def test_pick_lands_json_in_filehub(page, server):
    """指认一个不报错的元素 → json 落 inbox → 自动进待发标记。"""
    pos = page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag && window.__fp_diag.ring().length > 0")

    _long_press(page, 1700)
    expect(page.locator(".fp-diag-mask")).to_have_count(1)

    # ① 归属实时显示：指到哪，顶部条就写出它属于哪个组件
    page.mouse.move(pos["x"], pos["y"])
    page.wait_for_function("() => /messages\\.js/.test(document.querySelector('.fp-diag-bar .w').textContent)")

    # ② 点一下 = 立即记录（无确认卡、无必填项）
    page.mouse.click(pos["x"], pos["y"])

    expect(page.locator(".fp-diag-mask")).to_have_count(0)
    expect(page.locator("html[data-diag-frozen='1']")).to_have_count(0)
    expect(page.locator("#pendingFiles")).to_be_visible()

    label = page.locator("#pendingFiles .pending-file").first.inner_text()
    m = re.search(r"diag-[\w.-]+\.json", label)
    assert m, f"待发标记里没有诊断文件：{label!r}"

    rep = _inbox_json(server, m.group(0))
    assert rep["kind"] == "mark"
    assert "messages.js" in rep["where"]["comp"], rep["where"]["comp"]
    assert rep["where"]["tag"] == "div"
    assert rep["where"]["counts"]["children"] >= 1
    # 时间轴锚在「按下那一刻」：负偏移在前，0 附近是按下
    assert rep["timeline"], "时间轴为空——环里没东西，事后无法定位"
    assert min(e[0] for e in rep["timeline"]) < 0, "时间轴没有负偏移，说明锚点不是按下瞬间"
    # 冻结信息必须随报告走：我据此知道"这一屏是静止的"还是"边变边指"
    assert rep["env"]["frozen"] is True


def test_diag_can_be_disabled(page, server):
    """?diag=0 一键关闭（移动端没有控制台，这是唯一可用的关闭入口）。"""
    page.goto(server.base_url + "/?diag=0", wait_until="domcontentloaded")
    page.wait_for_timeout(600)
    assert page.evaluate("() => typeof window.__fp_diag") == "undefined"
    expect(page.locator(".status-bar")).to_be_visible()


# ── 两个被质疑过的点，各钉一条 ────────────────────────────────────────
# ① 「长按那 1.5 秒里 bug 跑了怎么办」→ 冻结提前到 pointerdown，阈值只管误触。
# ② 「事故不等手势」→ 异常抛出即上闸，屏幕停在出事那一屏，用户再从容指。
# ③ 「不是只支持移动端」→ 桌面 Alt+点击 也是一条完备路径。


def test_freeze_starts_on_pointerdown(page):
    """按下即冻：120ms 时（远小于 600ms 阈值）就该是冻结态。"""
    page.wait_for_function("() => window.__fp_diag")
    box = page.locator(".status-bar").bounding_box()
    assert box
    page.mouse.move(box["x"] + 24, box["y"] + box["height"] / 2)
    page.mouse.down()
    page.wait_for_timeout(120)
    assert page.evaluate("() => window.__fp_diag.frozen()") is True, "按下 120ms 还没冻——又回到「和 bug 赛跑」了"

    page.mouse.up()
    page.wait_for_timeout(100)
    assert page.evaluate("() => window.__fp_diag.frozen()") is False
    assert page.evaluate("() => window.__fp_diag.stashCount()") == 0, "回滚没把暂存帧放回去"
    # 静默回滚不留痕：普通点击状态栏不该在时间轴里留下 F 记录
    fs = page.evaluate("() => window.__fp_diag.ring().filter(e => e[1] === 'F').length")
    assert fs == 0, f"普通点击污染时间轴（{fs} 条 freeze 记录）"


def test_error_autolatches_then_pick(page, server):
    """异常抛出即刻上闸 → 小条浮出 → 直接指认，报告里留下 frozen_by=auto。"""
    pos = page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag && window.__fp_diag.ring().length > 0")

    page.evaluate("() => setTimeout(() => { throw new Error('boom-autolatch') }, 0)")
    expect(page.locator(".fp-diag-pill")).to_be_visible(timeout=3000)
    assert page.evaluate("() => window.__fp_diag.frozen()") is True
    assert page.evaluate("() => window.__fp_diag.autoReason()") == "js-error"

    page.locator(".fp-diag-pill button").first.click()  # 指认
    expect(page.locator(".fp-diag-mask")).to_have_count(1)
    page.mouse.click(pos["x"], pos["y"])

    expect(page.locator(".fp-diag-mask")).to_have_count(0)
    label = page.locator("#pendingFiles .pending-file").first.inner_text()
    m = re.search(r"diag-[\w.-]+\.json", label)
    assert m, f"待发标记里没有诊断文件：{label!r}"

    rep = _inbox_json(server, m.group(0))
    assert rep["env"]["frozen"] is True
    assert str(rep["env"]["frozen_by"]).startswith("auto:"), rep["env"]["frozen_by"]
    assert any(e[1] == "E" for e in rep["timeline"]), "时间轴里没有那条异常"


def test_ws_disconnect_does_not_autolatch(page):
    """断线**不得**自动上闸：手机切后台/锁屏必然掉 WS，那是常态不是事故。

    回归：曾把 close/error/half-open 一并当事故源，于是用户每次切个 App 回来
    都看到「⏸ 已自动冻结」+ 屏幕被冻住 8 秒——纯噪音，还把正常重连当成了 bug。
    断线仍要进时间轴（`C close` 是定位「界面卡住/丢帧」的关键证据），所以这条
    测试同时断言两件事：事件记下了、闸没动。
    """
    page.wait_for_function("() => window.__fp_diag && window.__fp_diag.ring().length > 0")
    page.evaluate("() => window.__fp_test.closeSocket()")
    # 等到 close 真的进了环再断言，否则断言可能跑在事件之前（假绿）
    page.wait_for_function(
        "() => window.__fp_diag.ring().some(e => e[1] === 'C' && e[2] === 'close')",
        timeout=5000,
    )
    assert page.evaluate("() => window.__fp_diag.frozen()") is False, "断线把屏幕冻住了"
    assert page.evaluate("() => window.__fp_diag.autoReason()") is None
    assert page.locator(".fp-diag-pill").count() == 0, "断线后浮出了自动冻结小条"


def test_alt_click_records_without_entering_mode(page, server):
    """桌面快捷：Alt+点击 一个元素直接记录，不进模式、不出遮罩。"""
    pos = page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag")
    page.keyboard.down("Alt")
    page.mouse.click(pos["x"], pos["y"])
    page.keyboard.up("Alt")

    expect(page.locator(".fp-diag-mask")).to_have_count(0)
    label = page.locator("#pendingFiles .pending-file").first.inner_text()
    m = re.search(r"diag-[\w.-]+\.json", label)
    assert m, f"Alt+点击 没产生诊断文件：{label!r}"
    rep = _inbox_json(server, m.group(0))
    assert "messages.js" in rep["where"]["comp"]
    assert rep.get("note", {}).get("via") == "alt-click"


def test_bar_is_floating_and_draggable(page):
    """悬浮菜单：不压住任何可交互控件，且能拖走。

    回归：原来是一条贴顶通栏横幅，盖住状态栏/头部——**报障工具挡住证据**，
    用户想指的东西可能就在被盖住的那一屏里。
    """
    page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag")
    _long_press(page, 900)

    bar = page.locator(".fp-diag-bar")
    expect(bar).to_have_count(1)

    # ① 与所有可见的按钮/链接/输入框都不相交
    clash = page.evaluate("""() => {
      const b = document.querySelector('.fp-diag-bar').getBoundingClientRect();
      const sel = 'button,a,input,textarea,[contenteditable="true"]';
      return [...document.querySelectorAll(sel)]
        .filter(n => !n.closest('.fp-diag-bar,.fp-diag-pill,.fp-diag-toast'))
        .filter(n => { const r = n.getBoundingClientRect();
          return r.width && r.height &&
                 b.left < r.right && b.right > r.left &&
                 b.top < r.bottom && b.bottom > r.top; })
        .map(n => n.id || n.className || n.tagName);
    }""")
    assert clash == [], f"悬浮菜单压住了控件：{clash}"

    # ② 拖得动（位置不合适时用户自己解决，不必求我改代码）
    r0 = bar.bounding_box()
    page.mouse.move(r0["x"] + 24, r0["y"] + 8)
    page.mouse.down()
    page.mouse.move(r0["x"] - 100, r0["y"] - 140, steps=8)
    page.mouse.up()
    r1 = bar.bounding_box()
    assert abs(r1["x"] - r0["x"]) > 20 or abs(r1["y"] - r0["y"]) > 20, f"拖不动：{r0} → {r1}"

    page.keyboard.press("Escape")
    expect(page.locator(".fp-diag-mask")).to_have_count(0)


def test_bar_avoids_controls_on_mobile_viewport(page):
    """手机视口（390x844）同样不压控件——用户主要在手机上用。"""
    page.set_viewport_size({"width": 390, "height": 844})
    page.reload(wait_until="domcontentloaded")
    page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag")
    _long_press(page, 900)
    expect(page.locator(".fp-diag-bar")).to_have_count(1)
    info = page.evaluate("""() => {
      const b = document.querySelector('.fp-diag-bar').getBoundingClientRect();
      const sel = 'button,a,input,textarea,[contenteditable="true"]';
      const clash = [...document.querySelectorAll(sel)]
        .filter(n => !n.closest('.fp-diag-bar,.fp-diag-pill,.fp-diag-toast'))
        .filter(n => { const r = n.getBoundingClientRect();
          return r.width && r.height &&
                 b.left < r.right && b.right > r.left &&
                 b.top < r.bottom && b.bottom > r.top; })
        .map(n => n.id || n.className || n.tagName);
      return { box: {x: Math.round(b.left), y: Math.round(b.top),
                     w: Math.round(b.width), h: Math.round(b.height)},
               vw: innerWidth, vh: innerHeight, clash };
    }""")
    assert info["clash"] == [], f"手机视口压住控件：{info}"
    assert info["box"]["w"] <= info["vw"], f"菜单超出屏幕宽度：{info}"


# ── 触屏：没有 hover，所以"点"只算选中，「确定」才落盘 ────────────────
# 需求原话：「手机没有hover，所以我希望click变成预览，然后悬浮窗内显示
# 确定按钮，实现选中」。分流看**这次输入的 pointerType**，不看设备类型——
# 触屏笔记本接鼠标时照样一点即记，不会因为"这台机器是触屏"就多一步确认。


@pytest.fixture()
def touch_page(server, browser):
    """触屏上下文（has_touch=True）+ 已登录。只有它能让 touchscreen.tap 生效。"""
    ctx = browser.new_context(has_touch=True, viewport={"width": 390, "height": 844}, device_scale_factor=3)
    pg = ctx.new_page()
    pg.set_default_timeout(15000)
    pg.goto(server.base_url + "/", wait_until="domcontentloaded")
    pg.fill("#authInput", server.token)
    pg.click("#authBtn")
    expect(pg.locator("#authOverlay")).to_be_hidden(timeout=20000)
    yield pg
    ctx.close()


def _move_bar_away(page, pt: dict) -> None:
    """浮窗若正压在目标点上，先拖到左上角——否则那一下会点到浮窗自己。
    用真实拖动（不是改样式），顺带再验一次拖动可用。"""
    b = page.locator(".fp-diag-bar").bounding_box()
    if not b:
        return
    inside = b["x"] <= pt["x"] <= b["x"] + b["width"] and b["y"] <= pt["y"] <= b["y"] + b["height"]
    if not inside:
        return
    page.mouse.move(b["x"] + 24, b["y"] + 8)
    page.mouse.down()
    page.mouse.move(8, 8, steps=6)
    page.mouse.up()


def test_touch_tap_previews_without_commit(touch_page):
    """触摸点一下 = 选中预览，**不**落盘：浮窗出「确定/重选」，归属已写出。"""
    pg = touch_page
    pos = pg.evaluate(PROBE)
    pg.wait_for_function("() => window.__fp_diag")
    _long_press(pg, 900)
    expect(pg.locator(".fp-diag-mask")).to_have_count(1)
    _move_bar_away(pg, pos)

    pg.touchscreen.tap(pos["x"], pos["y"])

    # 预览态的两个可观测特征：浮窗亮出动作区 + 元素被绿实线框锁定
    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(1)
    expect(pg.locator(".fp-diag-hl.sel")).to_have_count(1)
    # 但**没有**提交：模式还在、仍冻结、待发标记仍是空的
    expect(pg.locator(".fp-diag-mask")).to_have_count(1)
    assert pg.evaluate("() => window.__fp_diag.frozen()") is True, "预览期间不该解冻"
    assert pg.locator("#pendingFiles .pending-file").count() == 0, (
        "触摸点一下就直接提交了——手机上没有 hover，用户还没看见自己指了啥"
    )
    # 归属必须已经写在浮窗上（这就是"能确认指对了"的唯一依据）
    txt = pg.locator(".fp-diag-bar .w").inner_text()
    assert "messages.js" in txt, f"预览没写出归属：{txt!r}"


def test_touch_confirm_commits(touch_page, server):
    """点「确定」才落盘，且报告里记的是按下那一刻的快照。"""
    pg = touch_page
    pos = pg.evaluate(PROBE)
    pg.wait_for_function("() => window.__fp_diag")
    _long_press(pg, 900)
    _move_bar_away(pg, pos)
    pg.touchscreen.tap(pos["x"], pos["y"])
    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(1)

    pg.locator(".fp-diag-bar button.ok").click()

    expect(pg.locator(".fp-diag-mask")).to_have_count(0)
    expect(pg.locator("html[data-diag-frozen='1']")).to_have_count(0)
    label = pg.locator("#pendingFiles .pending-file").first.inner_text()
    m = re.search(r"diag-[\w.-]+\.json", label)
    assert m, f"确定后没有诊断文件：{label!r}"

    rep = _inbox_json(server, m.group(0))
    assert rep["kind"] == "mark"
    assert "messages.js" in rep["where"]["comp"], rep["where"]["comp"]
    assert rep["env"]["frozen"] is True
    assert rep["timeline"], "时间轴为空"


def test_touch_reselect_returns_to_picking(touch_page):
    """「重选」退回点选态：模式不退、冻结不断、也没留下任何提交。"""
    pg = touch_page
    pos = pg.evaluate(PROBE)
    pg.wait_for_function("() => window.__fp_diag")
    _long_press(pg, 900)
    _move_bar_away(pg, pos)
    pg.touchscreen.tap(pos["x"], pos["y"])
    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(1)

    pg.locator(".fp-diag-bar button.rt").click()

    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(0)
    expect(pg.locator(".fp-diag-hl.sel")).to_have_count(0)
    expect(pg.locator(".fp-diag-mask")).to_have_count(1)  # 仍在模式里
    assert pg.evaluate("() => window.__fp_diag.frozen()") is True
    assert pg.locator("#pendingFiles .pending-file").count() == 0

    # 重选之后还能再选一次（状态没卡死）
    pg.touchscreen.tap(pos["x"], pos["y"])
    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(1)


def test_mouse_still_commits_in_touch_device(touch_page, server):
    """同一台触屏设备接上鼠标 → 仍有 hover，一点即记，不弹「确定」。
    这条钉的是"按事件源分流"，防将来有人图省事改成按设备类型一刀切。"""
    pg = touch_page
    pos = pg.evaluate(PROBE)
    pg.wait_for_function("() => window.__fp_diag")
    _long_press(pg, 900)
    _move_bar_away(pg, pos)

    pg.mouse.click(pos["x"], pos["y"])

    expect(pg.locator(".fp-diag-mask")).to_have_count(0)
    expect(pg.locator(".fp-diag-bar.pick")).to_have_count(0)
    assert pg.locator("#pendingFiles .pending-file").count() >= 1, "触屏设备上用鼠标，反而被强制走预览了"


def test_no_toast_after_pick(page):
    """选中之后不许弹底部通知：它正好压住输入框，而待发标记本身就是回执。

    回归：原实现在 pick() 成功后 toast('✅ 已记录 …')，稳稳定定挡在输入框上——
    用户报障之后紧接着就要打字描述问题，这时候被自己刚触发的提示挡住。
    """
    pos = page.evaluate(PROBE)
    page.wait_for_function("() => window.__fp_diag")
    _long_press(page, 900)

    page.mouse.click(pos["x"], pos["y"])

    expect(page.locator(".fp-diag-mask")).to_have_count(0)
    expect(page.locator("#pendingFiles")).to_be_visible()  # 回执在待发标记里
    assert page.locator(".fp-diag-toast").count() == 0, "选中后又弹了悬浮通知（挡输入框）"
    assert page.locator("#pendingFiles .pending-file").count() >= 1


def test_bar_hides_internal_stash_counter(page):
    """浮窗只写「❄ 已冻结」，不暴露暂存帧数。

    回归：原来显示「❄ 已冻结 · N 帧暂存」。那个 N 是我确认"帧门真的在拦帧"用的
    内部计数，对用户没有意义，而且每涨一次都像在报"你的消息被吞了"。真溢出
    （丢过帧、补放还原不全）才该报警，那条由 stashDropped() 单独负责。
    """
    page.wait_for_function("() => window.__fp_diag")
    _long_press(page, 900)
    expect(page.locator(".fp-diag-bar")).to_have_count(1)

    txt = page.locator(".fp-diag-bar .t").inner_text()
    assert txt.strip() == "❄ 已冻结", f"浮窗暴露了内部计数：{txt!r}"
    assert "帧" not in txt, f"浮窗出现了「帧」字样：{txt!r}"

    page.keyboard.press("Escape")
    expect(page.locator(".fp-diag-mask")).to_have_count(0)
