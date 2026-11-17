/* ═══════════════════════════════════════════════════════════
   FP WebUI — ask 卡片（契约 v2）：状态驱动的结构化问答
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { ws, inputEl, addAssistantMessage, scrollToBottom, showError, setProcessing } from '../ui.js';
import { store } from '../core/store.js';

// ── ask 卡片（契约 v2）：等待中的结构化问答状态 ──
// replyAsk() 发送时携带 ask_id → 后端精确对账（幂等防重放）
// ── ask 卡片（契约 v2）：等待中的结构化问答状态 ──
// 状态归一化进 store.pendingAsk，渲染由订阅者幂等执行：同一状态渲染 N 次
// 结果一致。修掉实测 bug——旧实现无条件 addAssistantMessage，重连收到
// pending_ask 时会叠出第二张卡（且 liveMsgIndex 被推高两位）。
var _askContentEl = null;   // ask 卡片所在消息体（幂等渲染落点）
var _askSig = null;         // 上次渲染签名（幂等闸门：状态未变则整段跳过）

function buildAskCard(card, data) {
  var options = data.options || [];
  var suggest = data.suggest || '';
  while (card.firstChild) card.removeChild(card.firstChild);

  if (options.length) {
    var optWrap = document.createElement('div');
    optWrap.className = 'ask-options';
    options.forEach(function(opt) {
      var btn = document.createElement('button');
      btn.type = 'button';
      btn.className = 'ask-option' + (suggest && opt === suggest ? ' recommended' : '');
      btn.textContent = (suggest && opt === suggest ? '★ ' : '') + opt;
      btn.onclick = function() { replyAsk(opt); };
      optWrap.appendChild(btn);
    });
    card.appendChild(optWrap);
  }

  var foot = document.createElement('div');
  foot.className = 'ask-foot';
  // 键位说明交给下方的 .input-hint（按 touch/keyboard 切换），
  // 这里只讲"可以做什么"，否则触屏上「直接回车采纳」是错的说法
  foot.textContent = suggest
    ? '💡 推荐：' + suggest + '（或点上方按钮）'
    : '输入你的回答，或点上方选项';
  card.appendChild(foot);
}

// 纯渲染：只读状态，按状态画/撤卡片
function renderAskCard(state) {
  var data = state.pendingAsk;
  var card = document.getElementById('askCard');

  if (!data) {
    if (card) card.remove();
    _askContentEl = null;
    _askSig = null;
    inputEl.placeholder = '输入消息...';
    return;
  }

  // 幂等闸门：同一 ask 状态重复到达 → 不重建、不重复聚焦滚动
  var sig = [data.ask_id, data.prompt, data.suggest, (data.options || []).join('\u0000')].join('\u0001');
  if (card && sig === _askSig) return;
  _askSig = sig;

  if (!card) {
    // 整个流程里唯一一次建消息——之后只原地重建卡片内容
    _askContentEl = addAssistantMessage('❓ ' + (data.prompt || ''));
    card = document.createElement('div');
    card.className = 'ask-card';
    card.id = 'askCard';
    _askContentEl.appendChild(card);
  }
  buildAskCard(card, data);

  inputEl.placeholder = data.suggest
    ? ('采纳「' + data.suggest + '」，或输入回答...')
    : '输入回答...';
  inputEl.focus();
  scrollToBottom(true);
}

// 入口只改状态，渲染交给订阅者
export function showAskCard(data) {
  store.setState({ pendingAsk: {
    ask_id: data.ask_id || '',
    prompt: data.prompt || '',
    options: data.options || [],
    suggest: data.suggest || ''
  } });
}

export function clearAskCard() {
  store.setState({ pendingAsk: null });
}

// 状态 → 视图的唯一通道：状态变即重画（幂等）。subscribe 会立即以
// 当前状态回调一次，完成初始渲染。
store.subscribe(renderAskCard);

// 选项按钮/自由输入统一出口：发送 ask 答复（携带 ask_id 对账）
export function replyAsk(text) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { showError('未连接'); return; }
  var askId = (store.getState().pendingAsk || {}).ask_id || '';
  clearAskCard();
  // 不本地渲染：回答经 ask_user 环顶注入落盘 → ON_CTX_APPEND → user_message 事件渲染
  setProcessing(true);
  var payload = { type: 'message', content: text };
  if (askId) { payload.ask_id = askId; }
  try { ws.send(JSON.stringify(payload)); } catch (e) { showError('发送失败'); setProcessing(false); }
}