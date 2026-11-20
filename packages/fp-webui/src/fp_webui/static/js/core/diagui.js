/* ═══════════════════════════════════════════════════════════
   FP WebUI — 指认式报障界面（diagui）

   四个入口，**两端都算数**（不是"移动端专用"）：
     · 移动端：长按状态栏 600ms
     · 桌面：Ctrl+Shift+B
     · 桌面快捷：Alt+点击 目标元素（不进模式，直接记）
     · 事故自动：抛异常 / 断线 → 立刻上闸（不等任何手势），
       浮出「⏸ 已自动冻结 · 指认」小条，8 秒无人理会自动放开

   关于"长按那 1.5 秒里 bug 跑了"：**冻结从手指按下那一刻就开始**（投机冻结，
   松手是普通点击就静默回滚、补放暂存帧）。所以不存在"赛跑"——dom 在
   pointerdown 时就停住了。真正需要和事故赛跑的场合，交给自动上闸那条路：
   异常抛出瞬间就冻，用户随后从容指认。
   进入模式后：点一下出问题的地方（浮窗实时显示它属于哪个组件）
   → 记录并解冻，输入框里出现待发文件标记。

   点一下是"提交"还是"选中"，取决于**这次输入有没有 hover**（不是设备类型）：
     · 鼠标（有 hover）：指到哪儿，归属就实时写在浮窗上，点即提交，一步到位。
     · 触屏（无 hover）：手指按下去正好盖住要看的地方，松手前根本看不见自己
       指到了什么。所以触摸的"点"只做选中（绿实线框 + 浮窗写出归属），
       再按「确定」才落盘，「重选」重来。冻结保证选中的就是按下时那一个。
   按 pointerType 分流而不是 matchMedia，是为了触屏笔记本接鼠标时照样
   走即时提交，不因"这台机器是触屏"就多一步确认。

   为什么是"模式"而不是"长按任意元素直接标记"：移动端长按会撞上系统
   文本选择菜单（iOS 尤其顽固），跟浏览器抢手势不可靠。进一个"点哪儿
   都算我的"模式，比抢手势稳。

   为什么没有确认卡/症状按钮：用户要的是"指出"，不是填表。指完就完，
   想补充走普通消息即可。

   依赖方向：core → core（diag），不碰 features。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import {
  enterFreeze, exitFreeze, mark, describe,
  onAutoLatch, engage, autoReason, releaseAuto, frozen, stashDropped
} from './diag.js';

var STYLE_ID = 'fp-diag-style';
/* 长按阈值：只要够区分"点"和"按"。原来 1500ms 是拿用户的反应速度换误触率，
   太贵了——**冻结已经提前到 pointerdown**，阈值只剩一个作用：别把普通点击
   当成长按。600ms 足够。 */
var LONG_PRESS = 600;
var DRAG_PX = 8;         // 超过即判定框选而非点击

var _on = false;
var _mask = null, _hl = null, _bar = null, _where = null;
var _pill = null;        // 「⏸ 已自动冻结 · 指认」小条
var _start = null, _region = null;
var _tick = null;
/* 触屏预览态：{node, extra} —— 已选中但还没点「确定」。
   触屏没有 hover，用户在松手之前无法得知自己指到了什么，所以"点"只算预览，
   再点浮窗里的「确定」才写报告。桌面端（鼠标有 hover）仍是一点即记。 */
var _pending = null;
/* 本次按下是否来自无 hover 的输入（touch/pen）。按事件源判定而非设备类型：
   触屏笔记本接鼠标时照样走得通即时提交，不会因为"这台机器是触屏"就误走预览。 */
var _noHover = false;

/* ── 样式：自注入，独立于 CSS 文件（随包发布时不用动 css/）────── */
function ensureStyle() {
  if (document.getElementById(STYLE_ID)) return;
  var s = document.createElement('style');
  s.id = STYLE_ID;
  s.textContent = [
    /* 冻结：停掉动画（spinner 转圈也停），让这一屏真的静止 */
    'html[data-diag-frozen] *{animation-play-state:paused !important}',
    '.fp-diag-mask{position:fixed;inset:0;z-index:9998;cursor:crosshair;',
    'touch-action:none;-webkit-user-select:none;user-select:none;',
    '-webkit-touch-callout:none;background:rgba(0,0,0,.10)}',
    '.fp-diag-hl{position:fixed;z-index:9999;pointer-events:none;',
    'border:2px dashed #4da3ff;border-radius:4px;background:rgba(77,163,255,.10);',
    'box-sizing:border-box}',
    /* 触屏预览态：实线绿框 = 已选中待确认（虚线"悬停候选" vs 实线"已选"） */
    '.fp-diag-hl.sel{border-style:solid;border-color:#3fb950;',
    'background:rgba(63,185,80,.16)}',
    /* 悬浮菜单：不再是一条压住整幅的横幅。
       位置由 pickCorner() 在进入时现算（挑交互控件最少的角），且可拖动。
       为什么不用"贴顶通栏"：那会盖住状态栏/头部，用户想指的东西可能就在
       被盖住的那一屏里——报障工具挡住证据，是最讽刺的一种 bug。 */
    '.fp-diag-bar{position:fixed;left:0;top:0;z-index:10000;',
    'display:flex;gap:8px;align-items:center;padding:7px 10px 7px 8px;',
    'max-width:min(430px,72vw);',
    'background:rgba(15,20,25,.95);color:#e6edf3;border:1px solid rgba(255,255,255,.14);',
    'border-radius:12px;box-shadow:0 10px 32px rgba(0,0,0,.5);',
    'font:12px/1.5 ui-monospace,SFMono-Regular,Menlo,monospace;',
    '-webkit-backdrop-filter:blur(8px);backdrop-filter:blur(8px);',
    'touch-action:none;cursor:grab;-webkit-user-select:none;user-select:none}',
    '.fp-diag-bar.drag{cursor:grabbing}',
    '.fp-diag-bar .grip{flex:0 0 auto;color:#5d6771;letter-spacing:-1px;font-size:11px}',
    '.fp-diag-bar .t{flex:0 0 auto;color:#8ab4f8;font-weight:600}',
    '.fp-diag-bar .w{flex:1 1 auto;min-width:0;color:#c7d0d9;',
    'display:-webkit-box;-webkit-box-orient:vertical;-webkit-line-clamp:2;',
    'overflow:hidden;word-break:break-word}',
    '.fp-diag-bar .w b{color:#8ab4f8;font-weight:600}',
    '.fp-diag-bar .w i{color:#7a848e;font-style:normal}',
    '.fp-diag-bar button{flex:0 0 auto;background:#242b33;color:#e6edf3;border:0;',
    'border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}',
    /* 触屏预览态才出现的「确定/重选」。桌面端 hover 已看过，点即提交，不弹这两颗。 */
    '.fp-diag-bar .act{display:none;flex:0 0 auto;gap:6px}',
    '.fp-diag-bar.pick .act{display:flex}',
    '.fp-diag-bar button.ok{background:#1f6f3f;color:#e8fff0;font-weight:600}',
    '.fp-diag-bar button.rt{background:#2a333d;color:#b9c5d1}',
    /* 通知挪到顶部：底部那条正好压在输入框上，用户点名嫌它挡手。
       现在只剩"失败/没取到元素"这两种必须看见的提示才会弹。 */
    '.fp-diag-toast{position:fixed;left:50%;top:12px;transform:translateX(-50%);',
    'z-index:10001;background:#12301b;color:#b7f5c6;border:1px solid #2c5c3a;',
    'border-radius:8px;padding:9px 14px;font:13px/1.5 system-ui,sans-serif;',
    'max-width:88vw;box-shadow:0 6px 24px rgba(0,0,0,.35)}',
    '.fp-diag-toast.bad{background:#33181c;color:#ffc7d0;border-color:#6b2c36}',
    /* 事故自动闸门小条：右下角，不遮挡内容区，8 秒后自己消失 */
    '.fp-diag-pill{position:fixed;right:12px;bottom:12px;z-index:10001;',
    'display:flex;gap:8px;align-items:center;max-width:calc(100vw - 24px);',
    'background:#1b2330;color:#dbe5f0;border:1px solid #3a4a5f;border-radius:10px;',
    'padding:8px 10px;font:12px/1.5 system-ui,sans-serif;',
    'box-shadow:0 8px 28px rgba(0,0,0,.4)}',
    '.fp-diag-pill b{color:#8ab4f8}',
    '.fp-diag-pill span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;color:#9fb0c4}',
    '.fp-diag-pill button{flex:0 0 auto;background:#24354a;color:#dbe5f0;border:0;',
    'border-radius:6px;padding:4px 10px;font:inherit;cursor:pointer}',
    '.fp-diag-pill button.x{background:transparent;color:#7a848e}'
  ].join('');
  document.head.appendChild(s);
}

function el(tag, cls) {
  var e = document.createElement(tag);
  if (cls) e.className = cls;
  return e;
}

function isUi(node) {
  return !!(node && node.closest &&
    node.closest('.fp-diag-bar, .fp-diag-toast, .fp-diag-pill'));
}

/* 设备是否具备 hover。只用来定初始提示文案的措辞（"点一下" vs "点一下选中"），
   真正的分流看每次事件的 pointerType —— 混合设备（触屏笔记本）上两种输入
   都要各自走对，按设备类型一刀切会在那边出错。 */
function hoverCapable() {
  try { return !!(window.matchMedia && window.matchMedia('(hover: hover)').matches); }
  catch (e) { return true; }
}

/* ── 取最上层真实元素（跳过我们自己的覆盖层）────────────────── */
function topAt(x, y) {
  var list = document.elementsFromPoint(x, y) || [];
  for (var i = 0; i < list.length; i++) {
    var e = list[i];
    if (e.closest && e.closest('.fp-diag-mask, .fp-diag-bar, .fp-diag-hl, .fp-diag-toast, .fp-diag-pill')) continue;
    return e;
  }
  return null;
}

function drawBox(r) {
  if (!_hl) return;
  if (!r || (r.width <= 0 && r.height <= 0)) { _hl.style.display = 'none'; return; }
  _hl.style.display = 'block';
  _hl.style.left = r.left + 'px';
  _hl.style.top = r.top + 'px';
  _hl.style.width = r.width + 'px';
  _hl.style.height = r.height + 'px';
}

function drawEl(node) {
  if (!node || !node.getBoundingClientRect) { drawBox(null); return; }
  drawBox(node.getBoundingClientRect());
}

/** 顶部条：实时显示归属 —— 这就是"不知道错在哪个部分"的直接答案，
    用户在指的那一刻就能看见文件名。 */
function showWhere(node) {
  if (!_where) return;
  if (_pending) return;   // 预览态的文字归 setPending 独占，别被后来的移动冲掉
  if (!node) { _where.innerHTML = '<i>移到要指的地方…</i>'; return; }
  var d = describe(node);
  var counts = d.counts;
  var extra = counts ? ' <i>· ' + counts.assistant + ' 张卡 / ' + counts.children + ' 子节点</i>' : '';
  _where.innerHTML = '<b>' + esc(d.comp) + '</b> ' +
    '<i>' + esc(d.tag + (d.classes ? '.' + d.classes.split(/\s+/)[0] : '')) + '</i>' + extra;
}

function esc(s) {
  return String(s === undefined || s === null ? '' : s)
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
}

function refreshStash() {
  if (!_bar) return;
  var t = _bar.querySelector('.t');
  if (!t) return;
  /* 平时只写「❄ 已冻结」。原来这里是「❄ 已冻结 · N 帧暂存」——那是我自己
     用来确认"门确实在拦帧"的内部计数，对用户毫无用处，反而每次涨一下都像是在
     报"你的消息正在被吞掉"。界面不该暴露实现细节。
     只有真的溢出过（丢过帧，退出后补放还原不出完整画面）才值得说一句。 */
  t.textContent = stashDropped() ? '⚠ 暂存溢出' : '❄ 已冻结';
}

/* ── 悬浮菜单：落点与拖动 ────────────────────────────────
   菜单不能压住可交互控件——报障工具挡住证据，是最荒唐的一种 bug。
   进入时把四个角落都试一遍，选"被按钮/输入框占据最少"的那个；
   万一还是挡住了，拖走即可（用户此刻的判断永远比我的算法准）。 */
function uiRects() {
  var nodes = document.querySelectorAll(
    'button,a,input,textarea,select,summary,[contenteditable="true"]');
  var out = [];
  for (var i = 0; i < nodes.length; i++) {
    var n = nodes[i];
    if (n.closest && n.closest('.fp-diag-mask,.fp-diag-bar,.fp-diag-hl,.fp-diag-toast,.fp-diag-pill')) continue;
    var r = n.getBoundingClientRect();
    if (r.width > 0 && r.height > 0) out.push(r);
  }
  return out;
}

function hits(a, r) {
  return a.x < r.right && a.x + a.w > r.left && a.y < r.bottom && a.y + a.h > r.top;
}

function setBarPos(x, y) {
  if (!_bar) return;
  var w = _bar.offsetWidth || 260, h = _bar.offsetHeight || 40;
  var maxX = window.innerWidth - w - 4, maxY = window.innerHeight - h - 4;
  _bar.style.left = Math.max(4, Math.min(Math.round(x), Math.max(4, maxX))) + 'px';
  _bar.style.top = Math.max(4, Math.min(Math.round(y), Math.max(4, maxY))) + 'px';
}

function placeBar() {
  if (!_bar) return;
  var b = _bar.getBoundingClientRect();
  var w = b.width || 260, h = b.height || 44, m = 8;
  var vw = window.innerWidth, vh = window.innerHeight;
  var rects = uiRects();

  /* 四个角落不够用：手机竖屏时头和尾都被控件占满（实测 390x844 四角全撞
     attachBtn/inputField/sendBtn）。改成**网格扫描**——在整屏找真正的空位，
     重叠面积越大罚分越重，同分时轻微偏向右下（习惯位置）。
     560 个候选 × 几十个控件 ≈ 一万次矩形比较，进入模式时一次，可忽略。 */
  var best = { x: vw - w - m, y: vh - h - m }, bestScore = Infinity;
  var step = 24;
  for (var y = m; y <= vh - h - m; y += step) {
    for (var x = m; x <= vw - w - m; x += step) {
      var s = 0;
      for (var j = 0; j < rects.length; j++) {
        var r = rects[j];
        if (x < r.right && x + w > r.left && y < r.bottom && y + h > r.top) {
          var ox = Math.min(x + w, r.right) - Math.max(x, r.left);
          var oy = Math.min(y + h, r.bottom) - Math.max(y, r.top);
          s += 100 + (ox * oy) / 1000;      // 碰到就一定比不碰差
        }
      }
      s += (Math.abs(x - (vw - w - m)) + Math.abs(y - (vh - h - m))) / 5000;
      if (s < bestScore) { bestScore = s; best = { x: x, y: y }; }
    }
  }
  setBarPos(best.x, best.y);
}

function bindBarDrag() {
  if (!_bar) return;
  var down = false, px = 0, py = 0, ox = 0, oy = 0;
  _bar.addEventListener('pointerdown', function (e) {
    if (e.target.closest && e.target.closest('button')) return;
    down = true;
    px = e.clientX; py = e.clientY;
    var r = _bar.getBoundingClientRect();
    ox = r.left; oy = r.top;
    _bar.classList.add('drag');
    if (_bar.setPointerCapture) { try { _bar.setPointerCapture(e.pointerId); } catch (err) {} }
    e.preventDefault();
    e.stopPropagation();
  });
  _bar.addEventListener('pointermove', function (e) {
    if (!down) return;
    setBarPos(ox + e.clientX - px, oy + e.clientY - py);
    e.preventDefault();
    e.stopPropagation();
  });
  function end() {
    if (!down) return;
    down = false;
    _bar.classList.remove('drag');
  }
  _bar.addEventListener('pointerup', end);
  _bar.addEventListener('pointercancel', end);
}

/* ── 模式进出 ──────────────────────────────────────────── */

function enter() {
  if (_on) return;
  _on = true;
  ensureStyle();
  /* 如果已经因事故自动上闸，这一屏就是"出错那一屏"，别再动它：
     engage() 停掉 8 秒自动放行，让用户能从容指认。 */
  engage();
  removePill();
  enterFreeze();          // 未冻则此刻冻；已冻则原样保留（DOM 从按下/出事那刻起没变）

  _mask = el('div', 'fp-diag-mask');
  _hl = el('div', 'fp-diag-hl');
  _hl.style.display = 'none';
  _bar = el('div', 'fp-diag-bar');
  _bar.innerHTML = '<span class="grip">⠿</span>' +
    '<span class="t">❄ 已冻结</span>' +
    '<span class="w"><i>' + (hoverCapable()
      ? '点一下出问题的地方（拖动可框选）'
      : '点一下选中，再按「确定」（拖动可框选）') + '</i></span>' +
    '<span class="act">' +
      '<button type="button" class="ok">确定</button>' +
      '<button type="button" class="rt">重选</button>' +
    '</span>' +
    '<button type="button" class="x">退出</button>';
  _where = _bar.querySelector('.w');
  _bar.querySelector('.ok').addEventListener('click', function (e) {
    e.stopPropagation(); confirmPending();
  });
  _bar.querySelector('.rt').addEventListener('click', function (e) {
    e.stopPropagation(); clearPending();
  });
  _bar.querySelector('.x').addEventListener('click', function (e) {
    e.stopPropagation(); teardown();
  });

  document.body.appendChild(_mask);
  document.body.appendChild(_hl);
  document.body.appendChild(_bar);

  placeBar();             // 悬浮：挑交互控件最少的角落下
  bindBarDrag();          // 位置不合适就拖走

  document.addEventListener('pointerdown', onDown, true);
  document.addEventListener('pointermove', onMove, true);
  document.addEventListener('pointerup', onUp, true);
  document.addEventListener('pointercancel', onCancel, true);

  _tick = setInterval(refreshStash, 400);
}

function teardown() {
  if (!_on) return;
  _on = false;
  if (_tick) { clearInterval(_tick); _tick = null; }
  document.removeEventListener('pointerdown', onDown, true);
  document.removeEventListener('pointermove', onMove, true);
  document.removeEventListener('pointerup', onUp, true);
  document.removeEventListener('pointercancel', onCancel, true);
  [_mask, _hl, _bar].forEach(function (n) {
    if (n && n.parentNode) n.parentNode.removeChild(n);
  });
  _mask = _hl = _bar = _where = null;
  _start = _region = null;
  _pending = null;
  _noHover = false;
  releaseAuto();          // 清自动态（否则 engage 标记会让下次事故不再自动放行）
  exitFreeze();           // 解冻：暂存帧按序补放回去
}

/* ── 事件 ──────────────────────────────────────────────── */

function onDown(e) {
  if (isUi(e.target)) return;         // 放行浮窗自己的按钮
  e.preventDefault();
  e.stopPropagation();
  // 重新按下 = 放弃上一次预览（用户改主意，要指别的）
  if (_pending) clearPending(true);
  /* 分流依据是**这次输入**有没有 hover，不是这台设备是什么：
     touch/pen 没有 hover，松手前看不到自己指到了什么，所以"点"只算预览；
     mouse 有 hover，指的过程中归属已经实时显示，点即提交。 */
  _noHover = (e.pointerType === 'touch' || e.pointerType === 'pen');
  _start = { x: e.clientX, y: e.clientY };
  _region = null;
  // 按下瞬间就显示归属（不等移动）——这是"指"的信息核心
  var node = topAt(e.clientX, e.clientY);
  drawEl(node);
  showWhere(node);
}

function onMove(e) {
  if (!_on) return;
  /* 手指/光标靠近菜单时让它透明到几乎只剩个影子：
     万一它正好压在你想要的那块内容上，你仍然看得见底下是什么。
     不改成 pointer-events:none——那样就抓不住它拖不动了。 */
  if (_bar && !_bar.classList.contains('drag')) {
    var br = _bar.getBoundingClientRect();
    var near = e.clientX >= br.left - 12 && e.clientX <= br.right + 12 &&
               e.clientY >= br.top - 12 && e.clientY <= br.bottom + 12;
    _bar.style.opacity = near ? '0.22' : '';
  }
  // 预览已锁定：手指再怎么移动都不许改高亮和归属文字（锁定才有意义）
  if (_pending) return;
  if (isUi(e.target)) return;
  if (_start) {
    var dx = Math.abs(e.clientX - _start.x);
    var dy = Math.abs(e.clientY - _start.y);
    if (dx > DRAG_PX || dy > DRAG_PX) {
      _region = {
        left: Math.min(_start.x, e.clientX), top: Math.min(_start.y, e.clientY),
        width: dx, height: dy
      };
      drawBox(_region);
      showWhere(null);
      if (_where) {
        _where.innerHTML = '<i>框选 ' + Math.round(_region.width) + ' × ' +
          Math.round(_region.height) + ' px（松手记录这一片）</i>';
      }
    }
    return;
  }
  var node = topAt(e.clientX, e.clientY);
  drawEl(node);
  showWhere(node);
}

function onUp(e) {
  if (isUi(e.target)) return;
  e.preventDefault();
  e.stopPropagation();
  // 只认「在遮罩上按下过」的那次抬起（_start 由 onDown 设置）。
  // 原实现用时间窗（Date.now()-_enterAt < 250）挡"长按触发后松手那一下"，
  // 但这个猜测会两头出错：长按超过 1.75s 才松手 → 把状态栏那一点当成指认提交；
  // 而进模式后 200ms 内真的点了一个元素 → 反倒被忽略，用户以为记了其实没有。
  // 按下点归属是确定事实，直接用它判断。
  if (!_start) return;

  var region = _region;
  var touch = _noHover;
  _start = null;
  _region = null;
  _noHover = false;

  if (region && (region.width > DRAG_PX || region.height > DRAG_PX)) {
    var info = regionInfo(region);
    if (touch) setPending(info.anchor, info.extra);
    else pick(info.anchor, info.extra);
    return;
  }
  var node = topAt(e.clientX, e.clientY);
  if (touch) setPending(node, null);
  else pick(node, null);
}

function onCancel() { _start = null; _region = null; }

/* ── 提交 ──────────────────────────────────────────────── */

function pick(node, extra) {
  if (!node) {
    toast('没取到元素，再点一次试试', true);
    return;
  }
  // 顺序要紧：先 mark（同步快照，此刻 DOM 还是冻结的那一屏），
  // 再 teardown（解冻补放帧会让 DOM 变化）。反过来就指着"下一代"了。
  var r = mark(node, extra);
  teardown();
  /* 成功后不弹底部通知：输入框上方的待发文件标记本身就是回执，而那条 toast
     正好压在输入框上——报障工具挡住用户接下来要打的字，跟挡住证据一样荒唐。
     失败仍要弹（操作没生效却毫无反馈更糟），且已挪到顶部，不压输入框。 */
  r.promise.then(function (st) {
    if (!st || !st.ok) toast('⚠ 上传失败，已暂存本地', true);
  });
}

/** 框选：应对"该出现的东西没出现"、以及空白处被透明层挡住。
    九点采样，收这片区域里出现的所有不同元素，取前 12 个。
    只做"取信息"，不提交——触摸端要先预览再确认，鼠标端拿到就能直接 pick。 */
function regionInfo(r) {
  var xs = [r.left, r.left + r.width / 2, r.left + r.width];
  var ys = [r.top, r.top + r.height / 2, r.top + r.height];
  var seen = {}, list = [], center = null;
  for (var i = 0; i < xs.length; i++) {
    for (var j = 0; j < ys.length; j++) {
      var n = topAt(xs[i], ys[j]);
      if (!n) continue;
      var key = n.tagName + '|' + (typeof n.className === 'string' ? n.className : '');
      if (seen[key]) continue;
      seen[key] = 1;
      if (i === 1 && j === 1) center = n;
      var d = describe(n);
      list.push({ comp: d.comp, tag: d.tag, classes: d.classes, selector: d.selector });
      if (list.length >= 12) break;
    }
  }
  return {
    anchor: center || topAt(r.left + r.width / 2, r.top + r.height / 2),
    extra: {
      region: {
        x: Math.round(r.left), y: Math.round(r.top),
        w: Math.round(r.width), h: Math.round(r.height), elements: list
      }
    }
  };
}

/* ── 触屏预览态 ────────────────────────────────────────────
   手机没有 hover：手指按下去遮住的地方就是你正在指的地方，松手之后才知道
   指到了什么。所以触摸的"点"只做选中（绿实线框 + 浮窗写出归属），
   要再按「确定」才落盘。冻结保证预览期间那一屏不动，选中的就是按下时那一个。 */

function setPending(node, extra) {
  if (!node && !extra) {
    toast('没取到元素，再点一次试试', true);
    return;
  }
  _pending = { node: node, extra: extra };
  if (_hl) _hl.classList.add('sel');
  if (extra && extra.region) {
    var g = extra.region;
    drawBox({ left: g.x, top: g.y, width: g.w, height: g.h });
  } else {
    drawEl(node);
  }
  if (_bar) _bar.classList.add('pick');
  if (_where) {
    var what;
    if (extra && extra.region) {
      what = '框选 ' + extra.region.w + ' × ' + extra.region.h + ' px · ' +
        extra.region.elements.length + ' 个元素';
    } else {
      what = describe(node).comp;
    }
    _where.innerHTML = '<b>' + esc(what) + '</b> <i>· 按「确定」记录，或「重选」</i>';
  }
  relayoutBar();   // 多了两颗按钮 → 宽度变了，可能出屏，重新夹一下
}

function confirmPending() {
  var p = _pending;
  if (!p) return;
  _pending = null;
  if (_bar) _bar.classList.remove('pick');
  if (_hl) _hl.classList.remove('sel');
  // 冻结中 DOM 没动过，此刻 mark 拿到的仍是"按下那一刻"的那一屏
  pick(p.node, p.extra);
}

function clearPending(silent) {
  if (!_pending) return;
  _pending = null;
  if (_hl) { _hl.classList.remove('sel'); _hl.style.display = 'none'; }
  if (_bar) { _bar.classList.remove('pick'); relayoutBar(); }
  if (!silent && _where) {
    _where.innerHTML = '<i>' + (hoverCapable()
      ? '点一下出问题的地方（拖动可框选）'
      : '点一下选中，再按「确定」（拖动可框选）') + '</i>';
  }
}

/** 浮窗尺寸变了之后重新夹回屏内（保留当前落点，只防出界） */
function relayoutBar() {
  if (!_bar) return;
  var r = _bar.getBoundingClientRect();
  setBarPos(r.left, r.top);
}

function toast(msg, bad) {
  var t = el('div', 'fp-diag-toast' + (bad ? ' bad' : ''));
  t.textContent = msg;
  document.body.appendChild(t);
  setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 3600);
}

/* ── 入口 ──────────────────────────────────────────────── */

/** 长按状态栏：移动端主入口（不依赖任何权限，也不撞系统菜单） */
function bindLongPress() {
  var sb = document.querySelector('.status-bar') || document.getElementById('connectionStatus');
  if (!sb) { setTimeout(bindLongPress, 600); return; }
  var timer = null, sx = 0, sy = 0, spec = false;

  /* 投机冻结：手指按下就冻，不等阈值。
     这样"坏掉的那一屏"从**你决定要报的那一刻**就停住了——不存在
     "长按 1.5 秒里 bug 已经刷走"的赛跑。若这只是个普通点击（600ms 内
     松手/移动），静默回滚：解冻并补放暂存帧，用户完全感知不到。
     代价：点击状态栏会多一次 freeze/unfreeze（quiet，不写时间轴）。 */
  sb.addEventListener('pointerdown', function (e) {
    if (_on) return;
    sx = e.clientX; sy = e.clientY;
    if (!frozen()) { enterFreeze(true); spec = true; }
    if (timer) clearTimeout(timer);
    timer = setTimeout(function () { timer = null; spec = false; enter(); }, LONG_PRESS);
  });
  function cancel() {
    if (timer) { clearTimeout(timer); timer = null; }
    if (spec) { spec = false; exitFreeze(true); }   // 静默回滚，帧不丢
  }
  sb.addEventListener('pointerup', cancel);
  sb.addEventListener('pointercancel', cancel);
  sb.addEventListener('pointermove', function (e) {
    if (timer && (Math.abs(e.clientX - sx) > 10 || Math.abs(e.clientY - sy) > 10)) cancel();
  });
  // 触屏右键菜单会打断长按：状态栏上禁掉文本选择与呼唤
  sb.style.webkitUserSelect = 'none';
  sb.style.userSelect = 'none';
  sb.style.webkitTouchCallout = 'none';
}

/* ── 事故自动闸门的小条 ────────────────────────────────────
   异常/断线时浮出，把"此刻这一屏已经是静止的"告诉用户，
   并给一个直接进指认的按钮。8 秒没人管就自己消失。 */
function removePill() {
  if (_pill && _pill.parentNode) _pill.parentNode.removeChild(_pill);
  _pill = null;
}

function showPill(reason) {
  if (_on) return;                 // 已在模式里，无需小条
  ensureStyle();
  removePill();
  var p = el('div', 'fp-diag-pill');
  var label = {
    'js-error': '脚本报错', 'rejection': '异步异常'
  }[reason] || reason;
  p.innerHTML = '<b>⏸ 已自动冻结</b><span>' + label + ' · 屏幕已停在出事那一刻</span>' +
    '<button type="button">指认</button>' +
    '<button type="button" class="x">×</button>';
  p.querySelector('button').addEventListener('click', function (e) {
    e.stopPropagation(); e.preventDefault(); enter();
  });
  p.querySelector('.x').addEventListener('click', function (e) {
    e.stopPropagation(); e.preventDefault();
    removePill();
    releaseAuto();
    exitFreeze();                  // 用户说不用了，那就放行
  });
  document.body.appendChild(p);
  _pill = p;
}

/* ── 桌面快捷：Alt+点击 直接记一个元素（不进模式）─────────────
   为什么不是右键：右键是系统菜单，抢不过；Alt 是浏览器留给页面的。
   为什么不是"长按任意元素"：移动端长按撞系统文本选择菜单（前面已述）；
   桌面上 Alt+点击 就够快，一个动作完成"指出"。 */
function bindAltClick() {
  document.addEventListener('click', function (e) {
    if (!e.altKey || _on || isUi(e.target)) return;
    e.preventDefault();
    e.stopPropagation();
    var node = e.target;
    /* 先冻再快照：mark() 本身同步，但冻结能让 report 的 env.frozen 如实，
       也顺手阻止这次点击后面的业务 handler 改 DOM。 */
    var wasFrozen = frozen();
    if (!wasFrozen) enterFreeze(true);
    var r = mark(node, { via: 'alt-click' });
    if (!wasFrozen) exitFreeze(true);
    // 同上：成功不弹底部通知，待发标记就是回执
    r.promise.then(function (st) {
      if (!st || !st.ok) toast('⚠ 上传失败，已暂存本地', true);
    });
  }, true);
}

export function init() {
  bindLongPress();
  bindAltClick();
  onAutoLatch(function (phase) {
    if (phase === 'latched') showPill(autoReason());
    else if (phase === 'released') removePill();
  });
  document.addEventListener('keydown', function (e) {
    if (e.ctrlKey && e.shiftKey && (e.key === 'B' || e.key === 'b')) {
      e.preventDefault();
      if (_on) teardown(); else enter();
    } else if (e.key === 'Escape' && _on) {
      e.preventDefault();
      teardown();
    }
  });
}
