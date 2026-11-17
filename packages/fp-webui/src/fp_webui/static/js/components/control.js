/* ═══════════════════════════════════════════════════════════
   FP WebUI — 顶层控制
   setProcessing（运行/空闲态切换）+ clearUI（全量重置）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { messagesEl, inputEl, sendBtn, setLastGroupEl, setLiveMsgIndex, setToolContainerEl } from './state.js';
import { setSb, updateStatusBar } from './statusbar.js';
import { clearQueuedFlags, resetQueue } from './messages.js';
import { removeThinking } from './thinking.js';
import { detachStreamDom } from './stream.js';
import { resetToolCards } from './tools.js';
import { handlers } from '../core/handlers.js';
import { store } from '../core/store.js';

// processing 已迁 store.processing（读点走 store.getState().processing），
// 本模块只保留写入器 setProcessing。
export function setProcessing(state) {
  store.setState({ processing: state });
  // 运行中输入框保持可用（#158 排队发送），仅在结束时清除 ⏳ 标记
  if (!state) { clearQueuedFlags(); }

  if (state) {
    // 处理中 → 按钮变为中断按钮 ⏹️
    setSb({ state: 'processing' });
    sendBtn.disabled = false;
    sendBtn.textContent = '⏹️';
    sendBtn.style.background = '#8b3a3a';
    sendBtn.style.borderLeftColor = '#5a2a2a';
    sendBtn.onclick = function () { if (handlers.cancel) handlers.cancel(); };
    sendBtn.title = '中断 (Ctrl+Shift+C)';
    sendBtn.setAttribute('aria-label', '中断');
  } else {
    // 空闲 → 恢复为发送按钮 ➤
    setSb({ state: 'idle' });
    sendBtn.disabled = false;
    sendBtn.textContent = '➤';
    sendBtn.style.background = '';
    sendBtn.style.borderLeftColor = '';
    sendBtn.onclick = function () { if (handlers.send) handlers.send(); };
    sendBtn.title = '发送 (Enter)';
    sendBtn.setAttribute('aria-label', '发送消息');
    inputEl.focus();
  }
  updateStatusBar();
}

export function clearUI() {
  messagesEl.innerHTML = '';
  // 流式轮的 DOM 引用随 innerHTML 一起作废，必须同步摘掉：否则后续 token/chunk
  // 继续写进已脱离文档的「幽灵卡」——写是成功了，用户一个字也看不到
  //（重连 resync、agent 忙时切会话这两种场景都会撞上）。
  detachStreamDom();
  setLastGroupEl(null);
  setLiveMsgIndex(0);  // 重置索引计数器，后续由 fetchSessionHistory 重新设置
  resetToolCards();  // 清空工具卡片映射表
  setToolContainerEl(null);  // 工具行宿主随卡片一起作废
  resetQueue();  // 排队对账表随气泡一起作废（重拉/回溯后从新渲染）
  removeThinking();
  // 重置状态栏
  setSb({ toolsActive: 0, toolsTotal: 0, msgs: 0 });
  updateStatusBar();
}

