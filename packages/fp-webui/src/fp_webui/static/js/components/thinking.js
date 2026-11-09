/* ═══════════════════════════════════════════════════════════
   FP WebUI — 思考指示
   处理中 spinner + 进度条（元素状态模块私有）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { notifyUiStateChanged } from './state.js';
import { setSb, updateStatusBar } from './statusbar.js';
import { scrollToBottom } from './scroll.js';
import { getOrCreateMsgGroup } from './messages.js';

let thinkingEl = null;
let progressEl = null;

export function showThinking() {
  removeThinking();
  setSb({ state: 'thinking' });
  updateStatusBar();
  var group = getOrCreateMsgGroup();

  progressEl = document.createElement('div');
  progressEl.className = 'thinking-progress';
  progressEl.innerHTML = '<div class="thinking-progress-bar"></div>';
  group.appendChild(progressEl);

  thinkingEl = document.createElement('div');
  thinkingEl.className = 'thinking-indicator';
  var bars = '';
  for (var i = 0; i < 9; i++) { bars += '<span class="wave-bar"></span>'; }
  thinkingEl.innerHTML =
    '<span class="thinking-label">处理中</span>' +
    '<span class="thinking-wave" aria-label="处理中">' + bars + '</span>';
  group.appendChild(thinkingEl);

  scrollToBottom();
}

export function removeThinking() {
  if (thinkingEl && document.body.contains(thinkingEl)) { thinkingEl.remove(); thinkingEl = null; }
  if (progressEl && document.body.contains(progressEl)) { progressEl.remove(); progressEl = null; }
  notifyUiStateChanged();
}

