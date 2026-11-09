/* ═══════════════════════════════════════════════════════════
   FP WebUI — Agent 控制（热重启/清空/新建）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { ws, inputEl, addSystemMessage, clearUI, sessionLabel, modelBadge, messagesEl, scrollToBottom, showError } from '../ui.js';
import { authFetch } from '../core/http.js';
import { store } from '../core/store.js';
import { sendMessage } from './compose.js';
import { loadSessionsList } from './sessions.js';

// ── 重载 Agent（统一走命令面 /reload：进程级 execve 热重启，
//    落盘 → 重启 → 自动重连 → reload_done 提示，见 core/handoff 契约）──

export function reloadAgent() {
  if (store.getState().processing) { showError('正在处理中，请等待完成'); return; }
  if (!ws || ws.readyState !== WebSocket.OPEN) { showError('连接未就绪，请稍后重试'); return; }

  inputEl.value = '/reload';
  addSystemMessage('🔄 正在热重启（进程级）：落盘 → 重启 → 自动重连…');
  sendMessage();
}

export function clearSession() {
  if (!confirm('确定要清空当前会话吗？')) return;
  authFetch('/api/sessions/clear', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'cleared') {
        clearUI();
        sessionLabel.textContent = '📄 新会话';
        scrollToBottom(true);
      }
    })
    .catch(function(err) { showError('清空失败: ' + err.message); });
}

export function newAgent() {
  if (store.getState().processing) { showError('正在处理中，请等待完成'); return; }

  var btn = document.querySelector('.topbar-btn[onclick*="newAgent"]');
  var originalText = btn.textContent;
  btn.textContent = '⏳...';
  btn.disabled = true;

  authFetch('/api/agent/new', { method: 'POST' })
    .then(function(r) {
      if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'HTTP ' + r.status); });
      return r.json();
    })
    .then(function(data) {
      if (data.status === 'ok') {
        clearUI();
        sessionLabel.textContent = '📄 ' + (data.session_id || '—');
        modelBadge.textContent = data.model || '—';
        loadSessionsList();
        var msg = document.createElement('div');
        msg.className = 'msg-group';
        msg.innerHTML = '<div class="msg" style="justify-content:center;padding:16px 0"><span style="color:var(--text-dim);font-size:13px">🆕 已创建新 Agent（' + (data.model || '') + '）</span></div>';
        messagesEl.appendChild(msg);
        scrollToBottom(true);
      }
    })
    .catch(function(err) { showError('新建 Agent 失败: ' + err.message); })
    .finally(function() { btn.textContent = originalText; btn.disabled = false; });
}