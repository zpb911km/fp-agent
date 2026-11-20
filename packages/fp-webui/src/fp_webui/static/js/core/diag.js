/* ═══════════════════════════════════════════════════════════
   FP WebUI — 前端诊断环（diag）

   解决什么：UI 出问题时有两条路同时断掉——
     ① **不知道错在哪个部分**。前端无构建、ESM 直服源码，异常栈本可
        直指 `components/stream.js:128:7`，但没人接住，只落在用户自己
        的控制台里（移动端连控制台都没有）。
     ② **错误是动态的，看见了却来不及说**。闪一下、错一下、卡一下，
        用户叙述时那一刻已经过去，agent 只能通读源码猜。

   做法：内存里滚一个定长的「发生了什么」环（只记顺序与归属，不记
   正文——正文在 journal 里已有，塞进来会把体积炸掉）。用户指认一个
   元素时，连同**按下那一刻的快照**一起打包成 json，走已有 filehub
   （/api/files）落进 fp_inbox，用户按 ➤ 发送即可。agent 读一个 json
   就能定位到文件:行，不必通读源码。

   与 journal 的分工：journal 记「agent 语义」（llm_call / tool / save），
   在 core 钩子上；本文件记「前端收到的顺序 + 画成了什么」，在浏览器
   里。b771300「同一条回复显示两次」在 journal 里是完全干净的一轮，
   bug 在传输落地时序——journal 照不到这里。

   依赖分层：core 不许反向依赖 features/app —— 上传函数由入口注入
   （见 init()）。

   边界（刻意不做）：不变式断言、DOM 录制、store 全量快照、render 轨迹。
   字段留了扩展位，以后要加不改格式。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { getToken } from './http.js';
import { store } from './store.js';

var CAP = 500;          // 环容量：够装约 60 秒高频事件，约 200KB 内存
var SPAN_MS = 60000;    // 快照默认窗口

var _ring = [];
var _upload = null;     // 入口注入：(File) => Promise
var _frozen = false;    // 冻结门：true 时 WS 帧进暂存队列，不交给渲染
var _freezeLogged = false;  // 本次冻结是否已在时间轴留痕（防 quiet 后补记产生重复 F）
var _stash = [];        // 冻结期间暂存的原始帧（按到达顺序）
var _stashDropped = false;  // 暂存溢出过（丢了最旧的帧）→ 退出时补放无法还原全貌
var _replay = null;     // 解冻时的补放函数（ws.js 注册，避免本文件依赖 features）
var _lastKeys = '';     // store 结构指纹（只在**键集合**变化时记一条）

/* ── 时钟 ──────────────────────────────────────────────────
   一律用 performance.now()（单调、不受系统时钟跳变影响）。值是
   「页面加载起毫秒」，比 Date.now() 更适合做时间线排序。 */
function now() {
  return Math.round((typeof performance !== 'undefined' && performance.now)
    ? performance.now() : Date.now());
}

/** 记一条。变长数组 [t, kind, ...args]，kind 见下方各 push 点。 */
function push(kind) {
  var e = [now(), kind];
  for (var i = 1; i < arguments.length; i++) e.push(arguments[i]);
  _ring.push(e);
  if (_ring.length > CAP) _ring.shift();
}

/* ── 归属表：元素 → 组件 ────────────────────────────────────
   为什么不给 14 个组件逐个打 data-comp：那要改 14 处渲染代码，而且
   目标元素常是组件内部节点（不是根）。一张 10 行的表 + class 链已经
   足够定位；命中给文件名，未命中就靠 selector/html 我自己认。
   表随代码演进会偏，改 bug 时顺手维护即可——10 条，成本可忽略。 */
var COMP_MAP = [
  ['#messagesContainer .assistant-msg', 'components/messages.js · assistant 卡'],
  ['#messagesContainer .user-msg', 'components/messages.js · user 卡'],
  ['#messagesContainer .msg-group', 'components/messages.js · 消息分组'],
  ['#messagesContainer .tool-row', 'components/tools.js · 工具行'],
  ['#messagesContainer .thinking-indicator', 'components/thinking.js · 思考状态行'],
  ['#messagesContainer .thinking-progress', 'components/thinking.js · 思考块'],
  ['#messagesContainer .history-separator', 'components/history.js · 历史分隔'],
  ['#sidebar, #sessionsList', 'features/sessions.js · 侧边栏'],
  ['#inputField, #pendingFiles, .input-area', 'features/compose.js · 输入区'],
  ['#connectionStatus, .status-bar', 'components/statusbar.js'],
  ['#halo-canvas, #halo-bg', 'halo.js'],
  ['#authOverlay', 'features/auth（ui.js）']
];

/* ── 元素描述 ──────────────────────────────────────────── */

/** 短标识：tag#id.cls（用于祖辈链） */
function brief(el) {
  if (!el || el.nodeType !== 1) return '?';
  var s = el.tagName.toLowerCase();
  if (el.id) return s + '#' + el.id;
  var c = (typeof el.className === 'string' ? el.className : '').trim();
  if (c) s += '.' + c.split(/\s+/).slice(0, 2).join('.');
  return s;
}

/** 够短的稳定路径：最多 4 段，遇到 id 截断 */
function selector(el) {
  var parts = [], node = el, depth = 0;
  while (node && node.nodeType === 1 && depth < 4) {
    var s = node.tagName.toLowerCase();
    if (node.id) { parts.unshift(s + '#' + node.id); break; }
    var c = (typeof node.className === 'string' ? node.className : '').trim();
    if (c) s += '.' + c.split(/\s+/).slice(0, 2).join('.');
    var p = node.parentElement;
    if (p) {
      var same = Array.prototype.filter.call(p.children, function (x) {
        return x.tagName === node.tagName;
      });
      if (same.length > 1) s += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
    }
    parts.unshift(s);
    node = node.parentElement;
    depth++;
  }
  return parts.join(' > ');
}

/** 祖辈链（自内向外 6 层）——我据此判断"它长在哪、被谁包着" */
function chain(el) {
  var out = [], node = el, n = 0;
  while (node && node.nodeType === 1 && n < 6) {
    out.push(brief(node));
    node = node.parentElement;
    n++;
  }
  return out;
}

/** 同类计数——「重复了」这类症状的机器化身 */
function counts(el) {
  var c = el && el.closest ? el.closest('#messagesContainer') : null;
  if (!c) c = document.getElementById('messagesContainer');
  if (!c) return null;
  return {
    container: 'messagesContainer',
    children: c.children.length,
    msgs: c.querySelectorAll('.msg').length,
    assistant: c.querySelectorAll('.assistant-msg').length,
    tools: c.querySelectorAll('.tool-row').length
  };
}

/** 完整归属报告 */
function locate(el) {
  if (!el || el.nodeType !== 1) return { comp: '(无元素)', reason: 'no-element' };
  var comp = null, root = null;
  for (var i = 0; i < COMP_MAP.length; i++) {
    var m = el.closest ? el.closest(COMP_MAP[i][0]) : null;
    if (m) { comp = COMP_MAP[i][1]; root = m; break; }
  }
  var r = el.getBoundingClientRect ? el.getBoundingClientRect() : null;
  return {
    comp: comp || '(映射未命中，按 class 链与 html 判断)',
    comp_root: root ? brief(root) : null,
    tag: el.tagName.toLowerCase(),
    classes: (typeof el.className === 'string' ? el.className.trim() : ''),
    selector: selector(el),
    chain: chain(el),
    html: (el.outerHTML || '').slice(0, 300),
    box: r ? { x: Math.round(r.left), y: Math.round(r.top),
               w: Math.round(r.width), h: Math.round(r.height) } : null,
    counts: counts(el),
    text: (el.textContent || '').trim().slice(0, 80)
  };
}

/** 供 diagui 显示归属（同一套映射，避免两处各维护一份）。 */
export function describe(el) { return locate(el); }

/* ── 环境 ──────────────────────────────────────────────── */
function env() {
  var net = null;
  try {
    var c = navigator.connection || navigator.mozConnection || navigator.webkitConnection;
    if (c) net = { type: c.effectiveType || null, down: c.downlink || null };
  } catch (e) {}
  var ws = null;
  try {
    // 复用 e2e 接缝，避免 core 反向依赖 features/ws.js
    if (window.__fp_test && window.__fp_test.wsStats) ws = window.__fp_test.wsStats();
  } catch (e) {}
  return {
    vp: window.innerWidth + 'x' + window.innerHeight,
    dpr: window.devicePixelRatio || 1,
    vis: document.visibilityState,
    online: navigator.onLine,
    net: net,
    ws: ws,
    ua: (navigator.userAgent || '').slice(0, 140),
    frozen: _frozen,
    frozen_by: _frozen ? (_autoReason ? 'auto:' + _autoReason : 'user') : null,
    stashed: _stash.length
  };
}

/* ── 冻结门（指认期间让 DOM 静止）────────────────────────────
   为什么必须冻：webui 是 store 驱动重渲染（innerHTML 重写常见），流式
   输出时 DOM 每 100ms 重建一次。不冻的话用户点下去时，看到的节点和
   实际命中的节点可能已经不是"同一代"——这正是 d34593f 里那个幽灵
   节点的形状。冻结比在 dom 里找"上一代"便宜得多。

   注意：冻结是体验，**真正的保证是按下即快照**（mark 同步序列化）。
   两层冗余：冻结让"指哪是哪"成立，快照让"按下那一刻"永不丢失。 */
export function frozen() { return _frozen; }

export function stashCount() { return _stash.length; }

export function enterFreeze(quiet) {
  if (_frozen) {
    /* 已经冻着了（投机冻结 / 事故自动上闸），这次是"正式"进模式 → 补记一条。
       真实报告的教训：长按状态栏是 quiet 冻结，随后 enter() 的非 quiet 冻结
       因已冻直接 return，于是时间轴上冻结起点是一片空白——最后一条记录停在
       -24.8s，读报告的人会以为页面自己静默了半分钟，而不是"用户在这里进了
       指认模式"。_freezeLogged 保证只补一次，不产生重复 F。 */
    if (!quiet && !_freezeLogged) { _freezeLogged = true; push('F', 'freeze'); }
    return;
  }
  _frozen = true;
  _freezeLogged = !quiet;
  document.documentElement.setAttribute('data-diag-frozen', '1');
  // quiet：投机冻结（手指还没按满阈值）不留痕，否则点一下状态栏就多一条噪声
  if (!quiet) push('F', 'freeze');
}

export function exitFreeze(quiet) {
  if (!_frozen) return;
  _frozen = false;
  _freezeLogged = false;
  document.documentElement.removeAttribute('data-diag-frozen');
  if (!quiet) push('F', 'unfreeze', _stash.length);
  var st = _stash.splice(0);
  _stashDropped = false;
  if (_replay) {
    for (var i = 0; i < st.length; i++) {
      try { _replay(st[i]); } catch (e) {}
    }
  }
}

/** ws.js 的帧门：冻结期间吞进暂存队列并返回 true。 */
export function gateFrame(raw) {
  if (!_frozen) return false;
  _stash.push(raw);
  // 超限丢最旧：宁可少不可炸。但要记下来——一旦丢过，退出时的"按序补放"就
  // 还原不出完整画面了，这种情况值得让用户知道（也让我知道别信那份 DOM）。
  if (_stash.length > 400) { _stash.shift(); _stashDropped = true; }
  return true;
}

/** 暂存是否溢出过。UI 只在它为 true 时才提示——平时不暴露这个计数，
    它是我调试用的内部量，不是用户需要关心的东西。 */
export function stashDropped() { return _stashDropped; }

/** ws.js 注册补放函数（依赖方向：features → core）。 */
export function registerReplay(fn) { _replay = fn; }

/* ── 自动上闸（auto-latch）：解决"长按还没按满，bug 已经跑了" ──────
   异常抛出的那一瞬就冻结，**不等任何手势**。此后页面静止在出错的那一屏，
   用户有的是时间指。8 秒无人理会则自动解冻，不耽误正常使用。
   和长按入口的关系：长按求"用户主动"，auto-latch 求"事故主动"，
   两者共用同一条冻结/暂存通道，报告格式完全一致。

   触发源**只有 JS 异常与未捕获 Promise**。WS 断线/出错/半开一律不触发——那是
   手机切后台、锁屏、frp 抖动下的常态，见 connEvent 的注释。 */
var AUTO_LATCH_MS = 8000;
var _autoTimer = null;
var _autoReason = null;
var _autoTook = false;          // 用户是否接管（接管后不再自动解冻）
var _onLatch = null;            // diagui 注册：显示「⏸ 已自动冻结 · 指认」

export function onAutoLatch(cb) { _onLatch = cb; }

export function engaged() { return _autoTook; }

/** 用户接管：停掉自动解冻计时，冻结一直保持到指认完成或手动退出 */
export function engage() {
  _autoTook = true;
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  if (_onLatch) { try { _onLatch('engaged'); } catch (e) {} }
}

export function autoReason() { return _autoReason; }

/** 事故自动上闸。manual=false 时 8 秒后自动放行。 */
export function autoLatch(reason) {
  if (_frozen) {                 // 已经在冻：只补一条原因，别重入
    push('F', 'auto-again', reason);
    return false;
  }
  enterFreeze();
  _autoReason = reason;
  _autoTook = false;
  push('F', 'auto', reason);
  if (_autoTimer) clearTimeout(_autoTimer);
  _autoTimer = setTimeout(function () {
    _autoTimer = null;
    if (_autoTook) return;       // 用户正在指认，别把屏幕从手上抽走
    push('F', 'auto-release', reason);
    _autoReason = null;
    if (_onLatch) { try { _onLatch('released'); } catch (e) {} }
    exitFreeze();
  }, AUTO_LATCH_MS);
  if (_onLatch) { try { _onLatch('latched'); } catch (e) {} }
  return true;
}

/** 指认结束/用户退出：清掉自动态 */
export function releaseAuto() {
  if (_autoTimer) { clearTimeout(_autoTimer); _autoTimer = null; }
  _autoTook = false;
  _autoReason = null;
  if (_onLatch) { try { _onLatch('released'); } catch (e) {} }
}

/* ── 帧记录（ws.js 调用）───────────────────────────────── */
export function recvFrame(data) {
  if (!data || typeof data !== 'object') return;
  push('W', data.type || '?', data.seq === undefined ? null : data.seq,
       data.run_id || null);
}

/** 连接层事件：open / close / error / half-open —— 只记录，不上闸。
 *
 * **断线不是事故，是常态**：手机端浏览器切后台/锁屏就无法保持 WS（系统挂起
 * 页面），回来时必然走一次 close+重连。frp 链路上抖一下也一样。拿它上闸等于
 * 用户每次切个 App 回来都看到「⏸ 已自动冻结」，那是噪音，不是帮助。
 *
 * 所以断线只进时间轴（`C close` 是定位「界面卡住/丢帧」的关键证据），
 * 事故上闸只留给**真的抛了异常**那条路（js-error / rejection）。 */
export function connEvent(name, detail) {
  push('C', name, detail === undefined ? null : detail);
}

/** 通用交互事件 */
export function event(name, detail) {
  push('U', name, detail === undefined ? null : detail);
}

/* ── 快照 ──────────────────────────────────────────────── */

/** 取 [tMark-SPAN, tMark] 的窗口，时间轴重定位到按下那一刻（负数=之前）。 */
function windowSlice(tMark) {
  var from = tMark - SPAN_MS, out = [];
  for (var i = 0; i < _ring.length; i++) {
    var e = _ring[i];
    if (e[0] >= from && e[0] <= tMark) {
      var c = e.slice();
      c[0] = c[0] - tMark;      // 0 = 按下瞬间，负 = 之前
      out.push(c);
    }
  }
  return out;
}

function recentErrors() {
  var out = [];
  for (var i = _ring.length - 1; i >= 0 && out.length < 5; i--) {
    if (_ring[i][1] === 'E') out.push(_ring[i]);
  }
  return out;
}

function fileName() {
  var d = new Date();
  function p(n) { return (n < 10 ? '0' : '') + n; }
  return 'diag-' + d.getFullYear() + p(d.getMonth() + 1) + p(d.getDate()) + '-' +
    p(d.getHours()) + p(d.getMinutes()) + p(d.getSeconds()) + '-' +
    Math.random().toString(36).slice(2, 6) + '.json';
}

/* ── 指认：按下即快照 ──────────────────────────────────────
   返回 {name, promise}；报告**同步**构造完毕，之后 DOM 怎么变都不影响。 */
export function mark(el, extra) {
  var tMark = now();
  var rep = {
    v: 1,
    kind: 'mark',
    at: new Date().toISOString(),
    uptime_ms: tMark,
    where: locate(el),
    env: env(),
    errors: recentErrors(),
    timeline: windowSlice(tMark)
  };
  if (extra) rep.note = extra;

  var name = fileName();
  var text = JSON.stringify(rep);
  // 脱敏：token 绝不进落盘文件（b6e1869 有过 WS 握手日志泄 token 明文的前科）
  try {
    var tk = getToken();
    if (tk && tk.length > 6) text = text.split(tk).join('***');
  } catch (e) {}

  push('M', name);   // 马克本身也进环（便于下一次快照看到"上次指了哪"）

  var promise = Promise.resolve({ ok: false });
  if (_upload) {
    try {
      promise = Promise.resolve(
        _upload(new File([text], name, { type: 'application/json' }))
      ).then(function (res) {
        return res ? { ok: true, ref: res.ref } : { ok: false };
      }, function () { return { ok: false }; });
    } catch (e) {
      promise = Promise.resolve({ ok: false });
    }
  }

  // 上传失败不静默：留一份在 localStorage，下次 init 提示（frp 断网时常见）。
  // uploadFile 失败是 resolve(null) 而非 reject —— 所以这里统一走 ok 判定。
  var result = promise.then(function (st) {
    if (st.ok) return st;
    try {
      var q = JSON.parse(localStorage.getItem('fp_diag_unsent') || '[]');
      q.push({ name: name, text: text });
      localStorage.setItem('fp_diag_unsent', JSON.stringify(q.slice(-3)));
    } catch (e) {}
    return st;
  });

  return { name: name, promise: result };
}

/* ── 安装 ──────────────────────────────────────────────── */

/** 异常栈里挑出第一行 "static/js/...js:LINE:COL" —— 无构建，这就是源码行 */
function srcLine(stack) {
  var m = String(stack || '').match(/(?:static\/js\/|js\/)([\w./-]+\.js):(\d+):(\d+)/);
  return m ? (m[1] + ':' + m[2] + ':' + m[3]) : null;
}

function onError(e) {
  // 资源加载失败（img/script）也走 error 事件，但没有 error 对象：过滤掉
  var err = e && e.error;
  if (!err && !(e && e.message)) return;
  push('E', (err && err.message) || e.message || '?',
       srcLine(err && err.stack), e && e.filename ? (e.filename + ':' + e.lineno) : null);
  autoLatch('js-error');
}

function onRejection(e) {
  var r = e && e.reason;
  push('E', (r && (r.message || String(r))) || 'unhandledrejection', srcLine(r && r.stack), null);
  autoLatch('rejection');
}

/** 只观察 #messagesContainer 的**直接子节点增删**：
    流式正文更新只改文本、不改数量，所以几乎不产生记录；而「多出一张
    卡 / 少一张卡」必然改变数量——这是"重复了/少了/顺序错"三类的机器化身。 */
function watchDom() {
  var c = document.getElementById('messagesContainer');
  if (!c) { setTimeout(watchDom, 400); return; }
  var last = c.children.length;
  push('D', last, 0);
  new MutationObserver(function () {
    var n = c.children.length;
    if (n === last) return;
    last = n;
    push('D', n, c.querySelectorAll('.assistant-msg').length);
  }).observe(c, { childList: true });
}

/** store：只在**键集合**变化时记（新增/消失），忽略内容刷新。
    抓住「轮次开始/结束、processing 翻转」这类结构事件，代价近零。 */
function watchStore() {
  try {
    store.subscribe(function (s) {
      var keys = [];
      for (var k in s) {
        if (s[k] !== null && s[k] !== undefined && s[k] !== false && s[k] !== '') keys.push(k);
      }
      var sig = keys.join(',');
      if (sig === _lastKeys) return;
      _lastKeys = sig;
      push('S', sig);
    });
  } catch (e) {}
}

function watchInput() {
  document.addEventListener('click', function (e) {
    var t = e.target;
    if (!t || t.nodeType !== 1) return;
    // 跳过诊断层自己的点击：指认那一下会命中遮罩，记成 "click body" 会让我
    // 误判「用户点了背景」，而它其实是指认动作本身（另有 M 记录标记）。
    if (t.closest && t.closest('.fp-diag-mask, .fp-diag-bar, .fp-diag-toast')) return;
    push('U', 'click', selector(t));
  }, true);
  document.addEventListener('visibilitychange', function () {
    push('U', 'vis', document.visibilityState);
  });
  window.addEventListener('online', function () { push('U', 'net', 'online'); });
  window.addEventListener('offline', function () { push('U', 'net', 'offline'); });
}

/**
 * 启动。由入口（main.js）调用——core 不反向依赖 features，上传函数注入。
 * @param {{upload: (f: File) => Promise}} opts
 */
export function init(opts) {
  // 关闭开关：移动端没有控制台，?diag=0 是唯一可用的入口，故记住了下次也生效
  try {
    if (new URLSearchParams(location.search).get('diag') === '0') {
      localStorage.setItem('fp_diag', '0');
    }
    if (localStorage.getItem('fp_diag') === '0') return false;
  } catch (e) {}
  _upload = (opts && opts.upload) || null;
  window.addEventListener('error', onError, true);
  window.addEventListener('unhandledrejection', onRejection);
  watchDom();
  watchStore();
  watchInput();
  push('I', 'diag init', window.innerWidth + 'x' + window.innerHeight);

  // 上次上传失败留下的残片：提示一次（不自动重发，避免反复失败刷屏）
  try {
    var q = JSON.parse(localStorage.getItem('fp_diag_unsent') || '[]');
    if (q.length) {
      console.warn('[diag] 有 ' + q.length + ' 条未发出的诊断记录');
    }
  } catch (e) {}

  // 测试/调试接缝
  window.__fp_diag = {
    ring: function () { return _ring.slice(); },
    mark: mark,
    frozen: frozen,
    stashCount: stashCount,
    stashDropped: stashDropped,
    autoLatch: autoLatch,
    engage: engage,
    autoReason: autoReason
  };

  return true;
}
