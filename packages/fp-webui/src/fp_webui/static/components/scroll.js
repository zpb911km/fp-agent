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

export function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

// iOS 输入框 focus 时不自动缩放（已在 meta 中 user-scalable=no）
export function handleInputFocus() {
  // 某些 Android 浏览器需要额外处理
}

