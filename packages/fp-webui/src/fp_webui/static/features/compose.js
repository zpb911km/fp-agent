/* ═══════════════════════════════════════════════════════════
   FP WebUI — 消息编排（发送/键盘/回溯委托）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { messagesEl, inputEl, sessionId, autoResize, addUserMessage, addAssistantMessage, clearUI, setProcessing, setPendingBacktrack, showError, sessionLabel, closeCmdPalette, navigateCmdList, confirmCmdSelection, toggleCmdPalette, ws } from '../ui.js';
import { authFetch } from '../core/http.js';
import { store } from '../core/store.js';
import { replyAsk } from './ask.js';
import { loadSessionsList } from './sessions.js';
import { hasPendingFiles, consumePendingRefs } from './upload.js';
import { cancelMessage } from './ws.js';

// ── 发送消息 ──

// 回溯按钮事件委托
messagesEl.addEventListener('click', function(e) {
  var btn = e.target.closest('.backtrack-btn');
  if (!btn) return;
  var idx = parseInt(btn.dataset.index, 10);
  if (isNaN(idx)) return;
  var sid = sessionId;
  if (!sid) { showError('未指定当前会话'); return; }
  setPendingBacktrack(true);
  clearUI();
  setProcessing(true);
  try { ws.send(JSON.stringify({ type: 'message', content: '/back ' + idx + ' 2' })); } catch(e) { showError('发送失败'); }
});

export function sendMessage() {
  closeCmdPalette();
  var text = inputEl.value.trim();
  // 有待发文件时允许纯文件消息（正文可为空，引用即正文）
  if ((!text && !hasPendingFiles()) || !ws || ws.readyState !== WebSocket.OPEN) return;
  if (hasPendingFiles()) {
    text = consumePendingRefs() + (text ? '\n' + text : '');
  }

  // ask 答复通道：等待问答时，自由输入同样携带 ask_id 对账
  if (store.getState().pendingAsk) {
    inputEl.value = '';
    autoResize(inputEl);
    replyAsk(text);
    return;
  }

  // 运行中排队：输入框不禁用（#158），后端 submit() 入队、
  // 环顶穿插消费；回执 'queued' 渲染气泡 + ⏳ 标记，斜杠被拒回 'error'
  if (store.getState().processing) {
    inputEl.value = '';
    autoResize(inputEl);
    try { ws.send(JSON.stringify({ type: 'message', content: text })); } catch (e) { showError('发送失败'); }
    return;
  }

  var exitCmds = ['/exit', '/exit!', '/quit'];
  var trimmed = text.trim().toLowerCase();
  if (exitCmds.some(function(cmd) { return trimmed === cmd.trim(); })) {
    inputEl.value = '';
    autoResize(inputEl);
    // /exit 命令不落盘 → 两张卡均 ephemeral（不占索引、无回溯按钮）；
    // clearUI 会清空界面，这里只防 POST 失败时的索引污染
    addUserMessage(text, true);
    addAssistantMessage('🔄 检测到 `' + text.trim() + '`，自动新建会话…', true);
    authFetch('/api/sessions', { method: 'POST' }).then(function(r) {
      if (r.ok) {
        clearUI();
        r.json().then(function(data) {
          sessionLabel.textContent = '📄 ' + (data.session_id || '—');
          loadSessionsList();
        });
      }
    }).catch(function() {});
    return;
  }

  inputEl.value = '';
  autoResize(inputEl);
  // 不本地渲染：气泡由后端 user_message 事件驱动（直发经 ON_CTX_APPEND 广播，
  // 命令经 ON_COMMAND 发 ephemeral）→ 多设备同步、与历史视图一致
  setProcessing(true);
  try { ws.send(JSON.stringify({ type: 'message', content: text })); } catch(e) { showError('发送失败'); setProcessing(false); }
}

// ── 快捷命令面板 ──
export function handleKeyDown(event) {
  var palette = document.getElementById('cmdPalette');

  // Ctrl+/ 切换快捷命令面板
  if (event.ctrlKey && event.key === '/') {
    event.preventDefault();
    toggleCmdPalette();
    return;
  }

  // 面板打开时：键盘导航
  if (!palette.hidden) {
    if (event.key === 'ArrowDown') {
      event.preventDefault();
      navigateCmdList(1);
      return;
    }
    if (event.key === 'ArrowUp') {
      event.preventDefault();
      navigateCmdList(-1);
      return;
    }
    if (event.key === 'Enter') {
      event.preventDefault();
      confirmCmdSelection();
      return;
    }
    if (event.key === 'Escape') {
      event.preventDefault();
      closeCmdPalette();
      return;
    }
  }

  // Enter 发送
  if (event.key === 'Enter' && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
  // Ctrl+Shift+C 中断
  if (event.ctrlKey && event.shiftKey && (event.key === 'C' || event.key === 'c')) {
    event.preventDefault();
    if (store.getState().processing) cancelMessage();
  }
}