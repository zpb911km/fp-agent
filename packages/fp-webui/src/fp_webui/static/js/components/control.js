/* ═══════════════════════════════════════════════════════════
   FP WebUI — 顶层控制
   setProcessing（运行/空闲态切换）+ clearUI（全量重置）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { messagesEl, inputEl, sendBtn, stopBtn, setLastGroupEl, setLiveMsgIndex, setToolContainerEl } from './state.js';
import { setSb, updateStatusBar } from './statusbar.js';
import { clearQueuedFlags, resetQueue } from './messages.js';
import { removeThinking } from './thinking.js';
import { detachStreamDom } from './stream.js';
import { resetToolCards } from './tools.js';
import { handlers } from '../core/handlers.js';
import { store } from '../core/store.js';

// processing 已迁 store.processing（读点走 store.getState().processing），
// 本模块只保留写入器 setProcessing。
// ⏹️ 的点击绑定：与 sendBtn 的内联 onclick 不同，中断走 handlers 注册表
//（cancelMessage 在 ws.js，control 不能反向 import），查表延后到点击时求值。
stopBtn.onclick = function () { if (handlers.cancel) handlers.cancel(); };

// 忙时提示文案：只在触屏露出（CSS 管可见性，这里只管语义状态）
var busyHint = document.getElementById('busyHint');

export function setProcessing(state) {
  store.setState({ processing: state });
  // 运行中输入框保持可用（#158 排队发送），仅在结束时清除 ⏳ 标记
  if (!state) { clearQueuedFlags(); }

  // 忙/闲只切换「中断」按钮的可见性，➤ 本体永远是「发送」。
  // 曾经的写法是忙时把 ➤ 顶替成 ⏹️：桌面上还有 Enter 这条暗路可发（排队），
  // 触屏没有 Shift 键、Enter 只能换行 → agent 忙时**没有任何发送入口**。
  // 两个动作语义不同（一个是不可撤销的中断，一个是可排队的发送），
  // 就该是两个键；➤ 固定在最右，忙闲切换时位置不漂移。
  if (state) {
    setSb({ state: 'processing' });
    sendBtn.disabled = false;
    sendBtn.textContent = '➤';
    sendBtn.title = '发送（agent 忙时排队，本回合结束后投递）';
    sendBtn.setAttribute('aria-label', '发送消息（忙时排队）');
    // 中断键出现在 ➤ 左侧：可撤销的动作就近，不可撤销的靠里、需多伸一点
    stopBtn.hidden = false;
    stopBtn.disabled = false;
    stopBtn.textContent = '⏹️';
    if (busyHint) busyHint.hidden = false;
  } else {
    setSb({ state: 'idle' });
    sendBtn.disabled = false;
    sendBtn.textContent = '➤';
    sendBtn.title = '发送 (Enter)';
    sendBtn.setAttribute('aria-label', '发送消息');
    stopBtn.hidden = true;
    stopBtn.disabled = false;
    stopBtn.textContent = '⏹️';
    if (busyHint) busyHint.hidden = true;
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

