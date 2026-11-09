/* ═══════════════════════════════════════════════════════════
   FP WebUI — 状态栏
   状态源 = store.sb（setSb 归一写入，updateStatusBar 只读渲染）
   原 _sbData 就地改属性（引用不变、10 处写点散落 5 组件）已迁状态树，
   视图不再持有可变状态。从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { notifyUiStateChanged } from './state.js';
import { store } from '../core/store.js';

/**
 * 状态栏数据归一写入：patch 与 store.sb 浅合并。
 * @param {object} patch 如 {latency: 12}、{msgs: n}
 */
export function setSb(patch) {
  store.setState({ sb: Object.assign({}, store.getState().sb, patch) });
}

/** 读状态栏数据（只读快照，勿改）。 */
export function getSb() {
  return store.getState().sb;
}

export function updateStatusBar() {
  var latEl = document.getElementById('sbLatency');
  var toolsEl = document.getElementById('sbTools');
  var msgsEl = document.getElementById('sbMsgs');
  var stateEl = document.getElementById('sbState');
  if (!latEl || !toolsEl || !msgsEl || !stateEl) return;

  var _sbData = store.getState().sb;

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
