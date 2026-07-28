/* ═══════════════════════════════════════════════════════════
   Five Pebbles WebUI — 应用逻辑层
   从 index.html 拆分，2026-07-28
   包含: 认证 / WebSocket / 会话管理 / 初始化 / 键盘事件
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

// ── 避免 done.final_content 与 llm_end.content 重复渲染 ──
var _finalContentAlreadyShown = false;

// ── WebSocket 延迟测量 ──
var _lastPingTime = null;
var _pingInterval = null;

function withAuth(opts) {
  opts = opts || {};
  opts.headers = opts.headers || {};
  opts.headers['Authorization'] = 'Bearer ' + getToken();
  return opts;
}

async function authFetch(url, opts) {
  var res = await fetch(url, withAuth(opts));
  if (res.status === 401) {
    clearToken();
    showLogin();
    throw new Error('未授权');
  }
  return res;
}

// ── 登录 ──
function showLogin() {
  document.getElementById('authOverlay').style.display = 'flex';
  document.getElementById('authInput').value = '';
  document.getElementById('authError').classList.remove('show');
  document.getElementById('authBtn').disabled = false;
  setTimeout(function() { document.getElementById('authInput').focus(); }, 100);
}

function hideLogin() {
  document.getElementById('authOverlay').style.display = 'none';
}

async function doLogin() {
  var token = document.getElementById('authInput').value.trim();
  if (!token) return;
  var btn = document.getElementById('authBtn');
  var err = document.getElementById('authError');
  btn.disabled = true;
  err.classList.remove('show');
  try {
    var res = await fetch('/api/auth', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ token: token })
    });
    if (res.ok) {
      setToken(token);
      hideLogin();
      initApp();
    } else {
      err.textContent = '⛔ Token 无效，请重试';
      err.classList.add('show');
    }
  } catch (e) {
    err.textContent = '❌ 服务器连接失败';
    err.classList.add('show');
  }
  btn.disabled = false;
}

// ── 工具函数 ──
function cancelMessage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!processing) return;

  // 发送取消请求 → 后端 task.cancel() 注入 CancelledError
  ws.send(JSON.stringify({ type: 'cancel' }));

  // 按钮临时禁用，等待后端 cancelled 事件返回后再恢复
  sendBtn.disabled = true;
  sendBtn.textContent = '⏳';
  sendBtn.style.background = '#555';
  sendBtn.style.borderLeftColor = '#555';

  // 安全网：5 秒后如果后端没响应，强制恢复
  var cancelTimeout = setTimeout(function() {
    if (processing) setProcessing(false);
  }, 5000);
  // 存储 timeout id 以便 cancelled 事件到达时清理
  window._cancelTimeout = cancelTimeout;
}

function connectWebSocket() {
  if (ws && ws.readyState === WebSocket.OPEN) return;

  var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  var host = window.location.host;
  var token = getToken();
  var wsUrl = protocol + '//' + host + '/ws/chat?token=' + encodeURIComponent(token);

  ws = new WebSocket(wsUrl);

  ws.onopen = function() {
    statusEl.textContent = '🟢 已连接';
    setProcessing(false);
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      reconnectTimer = null;
    }
    // 启动延迟探测（每 10 秒 ping）
    if (_pingInterval) clearInterval(_pingInterval);
    _pingInterval = setInterval(function() {
      try {
        _lastPingTime = Date.now();
        ws.send(JSON.stringify({ type: 'ping' }));
      } catch(e) {}
    }, 10000);
    loadSessionsList();
  };

  ws.onclose = function() {
    statusEl.textContent = '🔴 已断开';
    setProcessing(false);
    _sbData.latency = null;
    updateStatusBar();
    if (_pingInterval) { clearInterval(_pingInterval); _pingInterval = null; }
    if (!reconnectTimer) {
      reconnectTimer = setTimeout(function() {
        reconnectTimer = null;
        connectWebSocket();
      }, 3000);
    }
  };

  ws.onerror = function() {
    statusEl.textContent = '🔴 连接错误';
  };

  ws.onmessage = function(event) {
    try {
      var data = JSON.parse(event.data);
      handleEvent(data);
    } catch (e) {
      console.error('[WS] 解析失败:', e);
    }
  };
}

function handleEvent(data) {
  var type = data.type;

  switch (type) {
    case 'connected':
      sessionId = data.sub_id;
      statusEl.textContent = '🟢 已连接';
      fetch('/api/health')
        .then(function(r) { return r.json(); })
        .then(function(h) {
          modelBadge.textContent = h.agent || '—';
          sessionLabel.textContent = '📄 ' + (h.session || '—');
        })
        .catch(function() {});
      break;

    case 'ping':
      try { ws.send(JSON.stringify({ type: 'pong' })); } catch(e) {}
      break;

    case 'pong':
      if (_lastPingTime) {
        _sbData.latency = Date.now() - _lastPingTime;
        _lastPingTime = null;
        updateStatusBar();
        // 闪烁动画
        var latEl = document.getElementById('sbLatency');
        if (latEl) { latEl.classList.remove('flash'); void latEl.offsetWidth; latEl.classList.add('flash'); }
      }
      break;

    case 'llm_start':
      showThinking();
      break;

    case 'llm_end':
      removeThinking();
      if (data.has_tool_calls) {
        if (data.content) {
          // LLM 有推理文本 + 工具调用 → 显示文本，后续 tool_select 显示工具选择
          addAssistantMessage(data.content);
        }
        // 纯 tool_calls 无文本 → 由 tool_select 显示选择记录，此处不输出
        // done.final_content 是最终答案，与 llm_end.content 不同 → 标记 false
        _finalContentAlreadyShown = false;
      } else if (data.content) {
        // 纯文本回复，无工具调用
        // done.final_content 与 data.content 相同 → 标记 true，避免 done 重复渲染
        addAssistantMessage(data.content);
        _finalContentAlreadyShown = true;
      } else {
        _finalContentAlreadyShown = false;
      }
      break;

    case 'tool_select':
      removeThinking();
      if (data.tools && data.tools.length > 0) {
        // 改为正式的 assistant-toolcall 消息，有头像、时间戳、回溯按钮
        addAssistantToolCallMsg(data.tools);
      }
      break;

    case 'tool_call':
      removeThinking();
      addToolCall(data.name, data.args, data.tool_call_id);
      break;

    case 'tool_result':
      addToolResult(data.name, data.result, false, data.tool_call_id);
      break;

    case 'error':
      removeThinking();
      showError(data.error);
      setProcessing(false);
      break;

    case 'done':
      removeThinking();
      setProcessing(false);
      // ── 渲染最终回复内容 ──
      // 命令和 LLM 回复统一通过 done.final_content 传递，
      // 不再走独立的 response 事件（参见 commits 71927ab）
      // 去重：纯文本回复（无工具调用）时 llm_end 已渲染过，跳过
      if (data.final_content && data.final_content !== '' && !_finalContentAlreadyShown) {
        addAssistantMessage(data.final_content);
      }
      // 重置标志，避免影响下一次
      _finalContentAlreadyShown = false;
      // ── 从后端获取权威索引计数，消除前端自增漂移 ──
      // 每次 done 事件都校准 liveMsgIndex，之后新的消息从正确起点继续累加。
      // 这样即使某条消息在保存过程中被跳过/压缩，索引也不会错位。
      if (data.non_system_count != null) {
        liveMsgIndex = data.non_system_count;
      }
      if (data.session_id) {
        sessionId = data.session_id;
        sessionLabel.textContent = '📄 ' + data.session_id;
      }
      if (_pendingBacktrack) {
        _pendingBacktrack = false;
        var curSid = data.session_id || sessionId;
        if (curSid) {
          setTimeout(function() { fetchSessionHistory(curSid); }, 100);
        }
      }
      break;

    case 'cancelled':
      removeThinking();
      // 取消 5 秒安全网定时器
      if (window._cancelTimeout) { clearTimeout(window._cancelTimeout); window._cancelTimeout = null; }
      setProcessing(false);
      addSystemMessage('⏹️ 已中断');
      break;

    case 'shutdown':
      statusEl.textContent = '🟠 服务器关闭中';
      setProcessing(false);
      break;

    case 'reload':
      statusEl.textContent = '🟠 Agent 重载中...';
      setProcessing(false);
      addSystemMessage('🔄 Agent 正在重载，连接即将断开...');
      break;

    case 'reload_done':
      statusEl.textContent = '🟢 已连接（已重载）';
      setProcessing(false);
      if (data.session_id) {
        sessionId = data.session_id;
        sessionLabel.textContent = '📄 ' + data.session_id;
      }
      if (data.model) { modelBadge.textContent = data.model; }
      addSystemMessage('🔄 Agent 重载完成，新代码已生效');
      break;

    case 'ask':
      removeThinking();
      addAssistantMessage('🔍 ' + data.prompt);
      setProcessing(false);
      inputEl.placeholder = '输入回复... (Enter 发送)';
      inputEl.focus();
      break;

    case 'info':
      addAssistantMessage('ℹ️ ' + data.content);
      break;

    case 'hint':
      var hintGroup = getOrCreateMsgGroup();
      var hintEl = document.createElement('div');
      hintEl.style.cssText = 'font-size:12px;color:var(--text-dim);padding:3px 0 3px calc(clamp(28px, 3vw, 36px) + clamp(8px, 1.5vw, 14px));font-style:italic;';
      hintEl.textContent = '💡 ' + data.content;
      hintGroup.appendChild(hintEl);
      scrollToBottom();
      break;

    case 'item':
      var itemGroup = getOrCreateMsgGroup();
      var itemEl = document.createElement('div');
      itemEl.style.cssText = 'font-size:13px;color:var(--text-secondary);padding:2px 0 2px calc(clamp(28px, 3vw, 36px) + clamp(8px, 1.5vw, 14px));font-family:var(--font-mono);';
      itemEl.textContent = data.content;
      itemGroup.appendChild(itemEl);
      scrollToBottom();
      break;

    case 'say':
      addAssistantMessage(data.content);
      break;
  }
}

// ── 发送消息 ──

// 回溯按钮事件委托
messagesEl.addEventListener('click', function(e) {
  var btn = e.target.closest('.backtrack-btn');
  if (!btn) return;
  var idx = parseInt(btn.dataset.index, 10);
  if (isNaN(idx)) return;
  var sid = sessionId;
  if (!sid) { showError('未指定当前会话'); return; }
  _pendingBacktrack = true;
  clearUI();
  setProcessing(true);
  try { ws.send(JSON.stringify({ type: 'message', content: '/back ' + idx + ' 2' })); } catch(e) { showError('发送失败'); }
});

function sendMessage() {
  closeCmdPalette();
  var text = inputEl.value.trim();
  if (!text || processing || !ws || ws.readyState !== WebSocket.OPEN) return;

  var exitCmds = ['/exit', '/exit!', '/quit'];
  var trimmed = text.trim().toLowerCase();
  if (exitCmds.some(function(cmd) { return trimmed === cmd.trim(); })) {
    inputEl.value = '';
    autoResize(inputEl);
    addUserMessage(text);
    addAssistantMessage('🔄 检测到 `' + text.trim() + '`，自动新建会话…');
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
  addUserMessage(text);
  lastGroupEl = null;
  setProcessing(true);
  try { ws.send(JSON.stringify({ type: 'message', content: text })); } catch(e) { showError('发送失败'); setProcessing(false); }
}

// ── 快捷命令面板 ──
function handleKeyDown(event) {
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
    if (processing) cancelMessage();
  }
}

// ── 会话管理 ──

function openSessions() {
  var modal = document.getElementById('sessionsModal');
  modal.classList.add('active');
  document.body.style.overflow = 'hidden';
  loadSessionsList();
}

function closeSessions() {
  document.getElementById('sessionsModal').classList.remove('active');
  document.body.style.overflow = '';
}

function loadSessionsList() {
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
        var isCurrent = s.is_current;
        var summary = s.summary || s.id.slice(0, 16) + '...';
        var created = s.created || '—';
        var msgCount = s.message_count || 0;
        html +=
          '<div class="session-item' + (isCurrent ? ' current' : '') + '" onclick="switchSession(\'' + s.id + '\')">' +
            '<div class="session-item-info">' +
              '<div class="session-item-summary">' + escapeHtml(summary) + '</div>' +
              '<div class="session-item-meta">' +
                escapeHtml(created) + ' · ' + msgCount + ' 条消息' +
              '</div>' +
            '</div>' +
            (isCurrent ? '<span class="session-item-badge">当前</span>' : '') +
            '<button class="session-load-btn" onclick="event.stopPropagation(); switchSession(\'' + s.id + '\')">' +
              (isCurrent ? '✓ 当前' : '切换到') +
            '</button>' +
            (!isCurrent ? '<button class="session-del-btn" onclick="event.stopPropagation(); deleteSession(\'' + s.id + '\', this)" title="删除会话" aria-label="删除会话">🗑</button>' : '') +
          '</div>';
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
        scrollToBottom();
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

// ── 重载 Agent ──

function reloadAgent() {
  if (processing) { showError('正在处理中，请等待完成'); return; }

  var btn = document.querySelector('.topbar-btn[onclick*="reloadAgent"]');
  var originalText = btn.textContent;
  btn.textContent = '⏳...';
  btn.disabled = true;

  authFetch('/api/reload', { method: 'POST' })
    .then(function(r) {
      if (!r.ok) return r.json().then(function(e) { throw new Error(e.detail || 'HTTP ' + r.status); });
      return r.json();
    })
    .then(function(data) {
      if (data.status === 'ok') {
        modelBadge.textContent = data.model || '—';
        sessionLabel.textContent = '📄 ' + (data.session_id || '—');
        fetch('/api/health')
          .then(function(r) { return r.json(); })
          .then(function(h) { modelBadge.textContent = h.agent || '—'; })
          .catch(function() {});
        loadSessionsList();
        addSystemMessage('🔄 Agent 重载完成');
      }
    })
    .catch(function(err) { showError('重载失败: ' + err.message); })
    .finally(function() { btn.textContent = originalText; btn.disabled = false; });
}

function clearSession() {
  if (!confirm('确定要清空当前会话吗？')) return;
  authFetch('/api/sessions/clear', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'cleared') {
        clearUI();
        sessionLabel.textContent = '📄 新会话';
        scrollToBottom();
      }
    })
    .catch(function(err) { showError('清空失败: ' + err.message); });
}

function newAgent() {
  if (processing) { showError('正在处理中，请等待完成'); return; }

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
        scrollToBottom();
      }
    })
    .catch(function(err) { showError('新建 Agent 失败: ' + err.message); })
    .finally(function() { btn.textContent = originalText; btn.disabled = false; });
}

function fetchSessionHistory(sid) {
  authFetch('/api/sessions/' + sid + '/messages')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      var msgs = data.messages || [];
      if (msgs.length > 0) {
        // 从尾部找最后一个有效 index（跳过 system 消息的 null index）
        liveMsgIndex = 0;
        for (var i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].index != null) {
            liveMsgIndex = msgs[i].index;
            break;
          }
        }
      }
      if (msgs.length > 0) {
        renderHistoryMessages(sid, msgs);
      }
    })
    .catch(function(err) { console.warn('[History] 加载历史失败:', err.message); });
}

function backtrackTo(sid, index) {
  if (!ws || ws.readyState !== WebSocket.OPEN) { showError('WebSocket 未连接'); return; }
  if (processing) { showError('正在处理中，请等待完成'); return; }
  _pendingBacktrack = true;
  clearUI();
  setProcessing(true);
  try { ws.send(JSON.stringify({ type: 'message', content: '/back ' + index + ' 2' })); } catch(e) { showError('发送失败'); }
}

// ── 重写 switchSession 以加载历史 ──
var _origSwitchSession = switchSession;
switchSession = function(sid) {
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
        scrollToBottom();
      }
    })
    .catch(function(err) { showError('切换会话失败: ' + err.message); });
};

// ── 主题切换 ──
function applyTheme(isLight) {
  var root = document.documentElement;
  var btn = document.getElementById('themeBtn');
  if (isLight) {
    root.classList.add('light-theme');
    if (btn) btn.innerHTML = '<span aria-hidden="true">🌙</span>';
    localStorage.setItem('webui_theme', 'light');
  } else {
    root.classList.remove('light-theme');
    if (btn) btn.innerHTML = '<span aria-hidden="true">☀️</span>';
    localStorage.setItem('webui_theme', 'dark');
  }
}

function toggleTheme() {
  var root = document.documentElement;
  applyTheme(!root.classList.contains('light-theme'));
}

// ── 初始化与登录流程 ──

function initApp() {
  // 恢复主题偏好
  var saved = localStorage.getItem('webui_theme');
  if (saved === 'light') applyTheme(true);
  connectWebSocket();
  inputEl.focus();
}

document.addEventListener('DOMContentLoaded', function() {
  if (isAuthed()) {
    initApp();
  } else {
    showLogin();
  }

  // 模态框外部点击关闭
  document.getElementById('sessionsModal').addEventListener('click', function(e) {
    if (e.target === e.currentTarget) closeSessions();
  });

  // ESC 关闭（命令面板 → 会话模态框）
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      if (!document.getElementById('cmdPalette').hidden) {
        closeCmdPalette();
      } else {
        closeSessions();
      }
    }
  });

  // 点击输入区外部关闭命令面板
  document.addEventListener('click', function(e) {
    if (document.getElementById('cmdPalette').hidden) return;
    if (!e.target.closest('.input-area')) {
      closeCmdPalette();
    }
  });

  // 窗口 resize 时保持滚动在底部（键盘弹出场景）
  var resizeTimer = null;
  window.addEventListener('resize', function() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      if (!processing) scrollToBottom();
    }, 150);
  });

  // 阻止 iOS 橡皮筋滚动导致页面整体上移
  document.body.addEventListener('touchmove', function(e) {
    if (e.target.closest('.messages-container') || e.target.closest('.modal-body')) return;
    // 允许这些容器内部滚动
  }, { passive: true });
});
