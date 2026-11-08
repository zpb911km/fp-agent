/* ═══════════════════════════════════════════════════════════
   FP WebUI — 状态栏
   _sbData + updateStatusBar（各组件改属性，引用不变）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { notifyUiStateChanged } from './state.js';

// ── 状态栏数据 ──
export var _sbData = {
  latency: null,
  toolsActive: 0,
  toolsTotal: 0,
  msgs: 0,
  state: 'idle'   // idle | thinking | processing
};

export function updateStatusBar() {
  var latEl = document.getElementById('sbLatency');
  var toolsEl = document.getElementById('sbTools');
  var msgsEl = document.getElementById('sbMsgs');
  var stateEl = document.getElementById('sbState');
  if (!latEl || !toolsEl || !msgsEl || !stateEl) return;

  // 延迟
  if (_sbData.latency != null) {
    var t = _sbData.latency;
    latEl.textContent = '⏱ ' + (t < 1000 ? t + 'ms' : (t/1000).toFixed(1) + 's');
  } else {
    latEl.textContent = '⏱ —';
  }

  // 工具
  var active = _sbData.toolsActive;
  var total = _sbData.toolsTotal;
  toolsEl.textContent = active > 0 ? '⚙ ' + active + '/' + total : '⚙ ' + total;
  if (active > 0) { toolsEl.classList.add('has-active'); }
  else { toolsEl.classList.remove('has-active'); }

  // 消息计数
  msgsEl.textContent = '✎ ' + _sbData.msgs;

  // 状态
  var s = _sbData.state;
  var icon = s === 'idle' ? '○' : s === 'thinking' ? '◌' : '◎';
  stateEl.textContent = icon + ' ' + s;
  stateEl.className = 'status-item state';
  if (s !== 'idle') stateEl.classList.add(s);
  notifyUiStateChanged();
}

