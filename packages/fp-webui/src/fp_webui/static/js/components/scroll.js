/* ═══════════════════════════════════════════════════════════
   FP WebUI — 视口
   跟随式滚底 + 输入框自适应（顶层挂 messagesEl scroll 监听）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { messagesEl } from './state.js';

// ── 跟随式滚底 ──
// 只有当用户本就贴近底部时才自动跟随（距底 <80px 视为贴底）；
// 上滑阅读历史即解除跟随，下滑回贴底部自动恢复。
// 「视图重置」类场景（加载历史/切会话/回溯/发送消息/启动）传 force=true 强制滚。
var FOLLOW_THRESHOLD_PX = 80;
var _followBottom = true;

function _isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < FOLLOW_THRESHOLD_PX;
}

messagesEl.addEventListener('scroll', function() {
  _followBottom = _isNearBottom();
});

export function scrollToBottom(force) {
  if (!force && !_followBottom) return;
  requestAnimationFrame(function() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
}

/* ── 输入框自适应高度 ────────────────────────────────────────
   · 上限不再写死 160：读 CSS 的 max-height（.input-field），让
     「最多显示几行」只有一处真相（媒体查询里移动端会放宽到 40vh）。
   · 超过上限后高度锁死 → 内容在框内滚动。此处必须保住用户的
     阅读位置：把 height 先置 'auto' 会临时把 scrollTop 夹掉，
     不还原的话长文本每敲一个字就跳一次（移动端表现为"滑不动/
     一滑就弹回去"）。贴底时保持贴底（正常输入跟随光标），
     否则还原用户拖到的位置。 */
export function autoResize(el) {
  var max = parseFloat(window.getComputedStyle(el).maxHeight);
  if (!isFinite(max) || max <= 0) max = 160;

  var prevTop = el.scrollTop;
  var wasAtBottom = el.scrollHeight - el.scrollTop - el.clientHeight < 2;

  el.style.height = 'auto';
  var target = Math.min(el.scrollHeight, max);
  el.style.height = target + 'px';

  if (el.scrollHeight > target) {   // 溢出：框内可滚，还原阅读位置
    el.scrollTop = wasAtBottom ? el.scrollHeight : prevTop;
  }
}

// iOS 输入框 focus 时不自动缩放（已在 meta 中 user-scalable=no）
export function handleInputFocus() {
  // 某些 Android 浏览器需要额外处理
}

