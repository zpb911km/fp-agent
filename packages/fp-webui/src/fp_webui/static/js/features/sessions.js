/* ═══════════════════════════════════════════════════════════
   FP WebUI — 会话管理（列表/搜索/切换/删除）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { escapeHtml, clearUI, sessionLabel, scrollToBottom, showError } from '../ui.js';
import { authFetch } from '../core/http.js';
import { fetchSessionHistory } from './history.js';

// ── 会话管理 ──

export function openSessions() {
  var modal = document.getElementById('sessionsModal');
  modal.classList.add('active');
  document.body.style.overflow = 'hidden';
  var si = document.getElementById('sessionsSearchInput');
  if (si) si.value = '';
  loadSessionsList();
}

export function closeSessions() {
  document.getElementById('sessionsModal').classList.remove('active');
  document.body.style.overflow = '';
}

/** 单条会话 DOM（列表与搜索结果共用；搜索结果额外带正文片段） */
function renderSessionItem(s, withSnippets) {
  var isCurrent = s.is_current;
  var when = s.created || s.updated || '—';
  var msgCount = s.message_count || 0;
  // 后端已保证 summary 可展示；此处仅兜底——不用 sid 冒充标题
  var summary = s.summary || (msgCount ? '（无摘要）' : '（空白会话）');
  // 关键词由后端 KL 管线预先算好（meta.kw），前端只读不算
  var kw = (s.kw && s.kw.length) ? s.kw.slice(0, 3).join(' · ') : '';
  var snippets = '';
  if (withSnippets) {
    (s.snippets || []).forEach(function(sn) {
      snippets += '<div class="session-item-snippet">' +
        '<span class="snip-role">' + (sn.role === 'user' ? '👤' : '🤖') + '</span>' +
        escapeHtml(sn.text) + '</div>';
    });
  }
  return '<div class="session-item' + (isCurrent ? ' current' : '') + '" data-sid="' + s.id + '">' +
    '<div class="session-item-info">' +
      '<div class="session-item-summary">' + escapeHtml(summary) + '</div>' +
      (kw ? '<div class="session-item-kw">' + escapeHtml(kw) + '</div>' : '') +
      snippets +
      '<div class="session-item-meta">' + escapeHtml(when) + ' · ' + msgCount + ' 条消息</div>' +
    '</div>' +
    (isCurrent ? '<span class="session-item-badge">当前</span>' : '') +
    '<button class="session-load-btn">' +
      (isCurrent ? '✓ 当前' : '切换到') +
    '</button>' +
    (!isCurrent ? '<button class="session-del-btn" title="删除会话" aria-label="删除会话">🗑</button>' : '') +
  '</div>';
}

var _sessionsSearchTimer = null;

export function onSessionsSearchInput() {
  var el = document.getElementById('sessionsSearchInput');
  var q = (el && el.value ? el.value : '').trim();
  clearTimeout(_sessionsSearchTimer);
  if (!q) { loadSessionsList(); return; }
  _sessionsSearchTimer = setTimeout(function() { runSessionsSearch(q); }, 250);
}

/** 搜正文（与终端 /find 同一套后端语义） */
function runSessionsSearch(q) {
  var list = document.getElementById('sessionsList');
  authFetch('/api/sessions/search?q=' + encodeURIComponent(q))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var results = data.results || [];
      if (!results.length) {
        list.innerHTML = '<div class="no-sessions">没有找到含「' + escapeHtml(q) + '」的会话</div>';
        return;
      }
      var html = '<div class="search-summary">『' + escapeHtml(q) + '』命中 ' + results.length + ' 个会话</div>';
      results.forEach(function(s) { html += renderSessionItem(s, true); });
      list.innerHTML = html;
    })
    .catch(function(err) {
      list.innerHTML = '<div class="no-sessions">搜索失败: ' + escapeHtml(err.message) + '</div>';
    });
}

export function loadSessionsList() {
  var list = document.getElementById('sessionsList');
  authFetch('/api/sessions')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      var sessions = data.sessions || [];
      if (sessions.length === 0) {
        list.innerHTML = '<div class="no-sessions">暂无历史会话</div>';
        return;
      }
      var html = '';
      sessions.forEach(function(s) {
        html += renderSessionItem(s, false);
      });
      list.innerHTML = html;
    })
    .catch(function(err) {
      list.innerHTML = '<div class="no-sessions">加载失败: ' + err.message + '</div>';
    });
}

function switchSession(sid) {
  authFetch('/api/sessions/' + sid + '/switch', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'switched') {
        clearUI();
        sessionLabel.textContent = '📄 ' + sid;
        closeSessions();
        loadSessionsList();
        fetchSessionHistory(sid);
        var msg = document.createElement('div');
        msg.className = 'msg-group';
        msg.style.borderBottom = 'none';
        msg.innerHTML = '<div class="msg" style="justify-content:center;padding:8px 0"><span style="color:var(--text-dim);font-size:12px">📂 已切换到 ' + sid.slice(0, 8) + '... 可发送消息继续对话</span></div>';
        document.getElementById('messagesContainer').appendChild(msg);
        scrollToBottom(true);
      }
    })
    .catch(function(err) { showError('切换会话失败: ' + err.message); });
}

function deleteSession(sid, btnEl) {
  if (!confirm('确定删除会话 ' + sid.slice(0, 12) + '... 吗？此操作不可恢复。')) return;
  btnEl.disabled = true;
  btnEl.style.opacity = '0.3';
  authFetch('/api/sessions/' + sid, { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'deleted') {
        var item = btnEl.closest('.session-item');
        if (item) { item.style.transition = 'opacity 0.3s'; item.style.opacity = '0'; }
        setTimeout(function() {
          if (item) item.remove();
          var list = document.getElementById('sessionsList');
          if (list && list.children.length === 0) {
            list.innerHTML = '<div class="no-sessions">暂无历史会话</div>';
          }
        }, 300);
      } else {
        btnEl.disabled = false;
        btnEl.style.opacity = '';
        showError('删除失败: ' + (data.detail || '未知错误'));
      }
    })
    .catch(function(err) {
      btnEl.disabled = false;
      btnEl.style.opacity = '';
      showError('删除会话失败: ' + err.message);
    });
}
/* ── 列表项动作：事件委托（原内联 onclick 在全局作用域求值，
   ESM 化后 switchSession/deleteSession 不再是 window 符号 → 点击即
   ReferenceError。改为容器委托：容器静态存在于 index.html，
   innerHTML 重绘（列表/搜索）不掉监听；单 handler 内先判按钮再判整卡，
   等价原 stopPropagation 的防双触发语义。）── */
document.getElementById('sessionsList').addEventListener('click', function (e) {
  var del = e.target.closest('.session-del-btn');
  if (del) { deleteSession(del.closest('.session-item').dataset.sid, del); return; }
  var item = e.target.closest('.session-item');
  if (item && item.dataset.sid) switchSession(item.dataset.sid);
});
