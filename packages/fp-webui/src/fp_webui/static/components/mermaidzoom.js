/* ═══════════════════════════════════════════════════════════
   FP WebUI — Mermaid 缩放
   滚轮/双指缩放 · 拖拽平移 · 双击复位 · 预览浮层（全自包含）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck


// ── Mermaid 缩放/平移（滚轮/双指缩放 · 拖拽平移 · 双击复位） ──
// 事件委托挂稳定祖先（消息区重渲染不丢监听）；状态存 WeakMap；
// 内联：只变换 svg（卡片盒内裁剪）；预览浮层：整张卡片一起变换（底/边框/阴影随内容走）；
// 参照盒与视口保持 ≥_mzEdge 像素重叠防"拖丢"。
var _mzState = new WeakMap();
var _mzMin = 0.2, _mzMax = 6, _mzEdge = 48;
var _mzDrag = null;
var _mzPts = {};      // 活动触点 id → {x,y}（双指缩放用）
var _mzPinch = null;  // { c, d0, s0 } 双指缩放起点
var _mzClick = null; // pointerdown 起点，供 click 区分「点开预览」与拖拽
var _mzOpenAt = 0;   // 浮层打开时刻（吞掉双击的第二击）

function _mzSt(container) {
  var st = _mzState.get(container);
  if (!st) { st = { s: 1, x: 0, y: 0 }; _mzState.set(container, st); }
  return st;
}

// 事件目标 → 图卡片；浮层暗背景上也归到浮层里唯一那张卡
function _mzCardFrom(t) {
  if (!t || !t.closest) return null;
  var c = t.closest('.mermaid');
  if (c) return c.querySelector('svg') ? c : null;
  var ov = t.closest('.mz-overlay');
  if (ov) { c = ov.querySelector('.mermaid'); return c && c.querySelector('svg') ? c : null; }
  return null;
}

// 被变换元素 + 参照盒：浮层变换整卡（背景跟随），内联只变换 svg
function _mzParts(c) {
  if (c.closest('.mz-overlay')) return { target: c, r0el: c.closest('.mz-stage') };
  return { target: c.querySelector('svg'), r0el: c };
}

function _mzApply(container) {
  var m = _mzParts(container);
  if (!m.target || !m.r0el) return;
  var st = _mzSt(container);
  var r0 = m.r0el.getBoundingClientRect();
  for (var pass = 0; pass < 2; pass++) {
    m.target.style.transformOrigin = '0 0';
    m.target.style.transform = 'translate(' + st.x + 'px,' + st.y + 'px) scale(' + st.s + ')';
    var r = m.target.getBoundingClientRect();
    var ox = r.left - r0.left, oy = r.top - r0.top;
    var dx = 0, dy = 0;
    var minX = _mzEdge - r.width, maxX = r0.width - _mzEdge;
    if (minX <= maxX) dx = Math.min(maxX, Math.max(minX, ox)) - ox;
    var minY = _mzEdge - r.height, maxY = r0.height - _mzEdge;
    if (minY <= maxY) dy = Math.min(maxY, Math.max(minY, oy)) - oy;
    if (!dx && !dy) break;
    st.x += dx; st.y += dy;
  }
  container.classList.toggle('mz-zoomed', st.s !== 1 || st.x !== 0 || st.y !== 0);
}

// 以锚点（光标/两指中点）缩放的公共数学
function _mzZoomBy(c, ns, ax, ay) {
  var st = _mzSt(c);
  ns = Math.min(_mzMax, Math.max(_mzMin, ns));
  if (ns === st.s) return;
  var m = _mzParts(c);
  var r = m.target.getBoundingClientRect(); // 旧变换下的原点
  var k = ns / st.s;
  st.x += (ax - r.left) * (1 - k);  // 锚点下的内容点保持不动
  st.y += (ay - r.top) * (1 - k);
  st.s = ns;
  _mzApply(c);
}

function _mzWheel(e) {
  if (e.target && e.target.closest && e.target.closest('.mz-close')) return;
  var c = _mzCardFrom(e.target);
  if (!c) return;
  e.preventDefault();
  var dY = e.deltaY;
  if (e.deltaMode === 1) dY *= 16; else if (e.deltaMode === 2) dY *= 100;
  var st = _mzSt(c);
  _mzZoomBy(c, st.s * Math.exp(-dY * 0.0018), e.clientX, e.clientY);
}

function _mzDown(e) {
  if (e.button !== 0 || !e.target.closest) return;
  var c = _mzCardFrom(e.target);
  if (!c) return;
  _mzClick = { x: e.clientX, y: e.clientY };
  var inOv = !!c.closest('.mz-overlay');
  // 未缩放不劫持（保留文字选择）；预览浮层内例外（浮层专为缩放平移而生）
  if (!inOv && !c.classList.contains('mz-zoomed')) return;
  e.preventDefault();
  _mzPts[e.pointerId] = { x: e.clientX, y: e.clientY };
  if (Object.keys(_mzPts).length >= 2) {          // 第二指落下 → 双指缩放，压掉单指拖拽
    if (_mzDrag) { _mzDrag.c.classList.remove('mz-dragging'); _mzDrag = null; }
    if (!_mzPinch) _mzPinch = { c: c, d0: _mzPinchD0(), s0: _mzSt(c).s };
    return;
  }
  _mzDrag = { c: c, id: e.pointerId, x: e.clientX, y: e.clientY };
  c.classList.add('mz-dragging');
}

function _mzPinchPairs() {
  var ids = Object.keys(_mzPts);
  if (ids.length < 2) return null;
  return [_mzPts[ids[0]], _mzPts[ids[1]]];
}

function _mzPinchD0() {
  var p = _mzPinchPairs();
  if (!p) return 1;
  return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
}

function _mzMove(e) {
  if (_mzPts[e.pointerId]) _mzPts[e.pointerId] = { x: e.clientX, y: e.clientY };
  if (_mzPinch) {
    var p = _mzPinchPairs();
    if (!p) return;
    var d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
    _mzZoomBy(_mzPinch.c, _mzPinch.s0 * d / _mzPinch.d0, (p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2);
    return;
  }
  if (!_mzDrag || e.pointerId !== _mzDrag.id) return;
  var st = _mzSt(_mzDrag.c);
  st.x += e.clientX - _mzDrag.x;
  st.y += e.clientY - _mzDrag.y;
  _mzDrag.x = e.clientX; _mzDrag.y = e.clientY;
  _mzApply(_mzDrag.c);
}

function _mzUp(e) {
  delete _mzPts[e.pointerId];
  if (_mzPinch) {
    var n = Object.keys(_mzPts).length;
    if (n >= 2) return;                        // 还有两指在，继续缩放
    var pc = _mzPinch.c;
    _mzPinch = null;
    if (n === 1) {                             // 剩一指 → 无缝转平移
      var id = Object.keys(_mzPts)[0];
      _mzDrag = { c: pc, id: id, x: _mzPts[id].x, y: _mzPts[id].y };
      pc.classList.add('mz-dragging');
    } else {
      pc.classList.remove('mz-dragging');
    }
    return;
  }
  if (!_mzDrag || e.pointerId !== _mzDrag.id) return;
  _mzDrag.c.classList.remove('mz-dragging');
  _mzDrag = null;
}

function _mzDbl(e) {
  var c = _mzCardFrom(e.target);   // 浮层暗背景双击同样复位那张卡
  if (!c) return;
  e.preventDefault();
  _mzState.set(c, { s: 1, x: 0, y: 0 });
  _mzApply(c);
}

export function initMermaidZoom(container) {
  if (!container || container._mzBound) return;
  container._mzBound = true;
  container.addEventListener('wheel', _mzWheel, { passive: false });
  container.addEventListener('pointerdown', _mzDown);
  container.addEventListener('click', _mzClickOpen);
  container.addEventListener('dblclick', _mzDbl);
  window.addEventListener('pointermove', _mzMove);
  window.addEventListener('pointerup', _mzUp);
  window.addEventListener('pointercancel', _mzUp);
}

// ── 点开预览（单击未缩放的图 → 全屏浮层 · 滚轮/双指缩放 · 拖拽平移 · 双击复位 · Esc/✕/点空白关闭） ──
function _mzClickOpen(e) {
  var info = _mzClick;
  _mzClick = null;
  if (!info || Math.abs(e.clientX - info.x) > 5 || Math.abs(e.clientY - info.y) > 5) return; // 拖拽残留的 click 不开
  if (e.button !== undefined && e.button !== 0) return;
  var c = e.target && e.target.closest ? e.target.closest('.mermaid') : null;
  if (!c || !c.querySelector('svg')) return;
  if (c.closest('.mz-overlay')) return;               // 浮层内点击不开嵌套预览
  if (c.classList.contains('mz-zoomed')) return;      // 已缩放 = 平移/双击复位模式
  var sel = window.getSelection && window.getSelection();
  if (sel && String(sel)) return;                     // 正在选中文本不误开
  openMzPreview(c);
}

function openMzPreview(c) {
  closeMzPreview();
  var ov = document.createElement('div');
  ov.className = 'mz-overlay';
  var stage = document.createElement('div');
  stage.className = 'mz-stage';
  var clone = c.cloneNode(true);                      // 克隆已渲染 svg（data-processed 随迁，不触发重渲染）
  clone.classList.remove('mz-zoomed', 'mz-dragging');
  stage.appendChild(clone);
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'mz-close';
  btn.setAttribute('aria-label', '关闭预览');
  btn.textContent = '✕';
  ov.appendChild(stage);
  ov.appendChild(btn);
  document.body.appendChild(ov);
  _mzOpenAt = Date.now();
  initMermaidZoom(stage);                             // 浮层复用同一套 wheel/pointer/dbl/click 委托
  btn.addEventListener('click', closeMzPreview);
  ov.addEventListener('click', function (ev) {        // 点空白（stage 背景/遮罩）关闭
    if (ev.target === ov || ev.target === stage) {
      if (Date.now() - _mzOpenAt < 300) return;       // 双击的第二击不误关
      closeMzPreview();
    }
  });
  document.addEventListener('keydown', _mzEsc);
}

function closeMzPreview() {
  var ov = document.querySelector('.mz-overlay');
  if (ov) ov.remove();
  document.removeEventListener('keydown', _mzEsc);
}

function _mzEsc(e) {
  if (e.key === 'Escape') closeMzPreview();
}
