/* ═══════════════════════════════════════════════════════════
   FP WebUI — UI 渲染层
   从 index.html 拆分，2026-07-28
   包含: 共享状态 / DOM 渲染 / 命令面板 / 消息构建
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

/* ═══════════════════════════════════════════════════════════
   FP WebUI — 前端逻辑
   ═══════════════════════════════════════════════════════════ */

// ── 状态 ──
let ws = null;
let sessionId = null;
let processing = false;
let reconnectTimer = null;
let lastGroupEl = null;
let thinkingEl = null;
let progressEl = null;

// ── 工具调用追踪 —— tool_call_id → DOM 元素 ──
// 解决并行调用同名工具时按名称匹配错乱的问题
var toolCardMap = {};

// ── 状态栏数据 ──
var _sbData = {
  latency: null,
  toolsActive: 0,
  toolsTotal: 0,
  msgs: 0,
  state: 'idle'   // idle | thinking | processing
};

function updateStatusBar() {
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
}

// ── DOM 引用 ──
const messagesEl = document.getElementById('messagesContainer');
const inputEl = document.getElementById('inputField');
const sendBtn = document.getElementById('sendBtn');
const sessionLabel = document.getElementById('sessionLabel');
const modelBadge = document.getElementById('modelBadge');
const statusEl = document.getElementById('connectionStatus');

// ── 认证 ──
const AUTH_KEY = 'webui_token';
function getToken() { return sessionStorage.getItem(AUTH_KEY) || ''; }
function setToken(t) { sessionStorage.setItem(AUTH_KEY, t); }
function clearToken() { sessionStorage.removeItem(AUTH_KEY); }
function isAuthed() { return !!getToken(); }

function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

function formatTime(ts) {
  var d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}

function scrollToBottom() {
  requestAnimationFrame(function() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  });
}

function autoResize(el) {
  el.style.height = 'auto';
  el.style.height = Math.min(el.scrollHeight, 160) + 'px';
}

// iOS 输入框 focus 时不自动缩放（已在 meta 中 user-scalable=no）
function handleInputFocus() {
  // 某些 Android 浏览器需要额外处理
}

// ── 消息渲染 ──

function renderMarkdown(content) {
  if (!content) return '';
  if (typeof marked === 'undefined') {
    return escapeHtml(content).replace(/\n/g, '<br>');
  }
  try {
    var html = marked.parse(content, { breaks: true, gfm: true });
    // DOMPurify 清洗 HTML，移除恶意标签/属性
    if (typeof DOMPurify !== 'undefined') {
      html = DOMPurify.sanitize(html);
    } else {
      // DOMPurify 不可用时，降级到 escapeHtml（牺牲格式保安全）
      return escapeHtml(content).replace(/\n/g, '<br>');
    }
    // 包裹所有 <table> 使其可横向滚动
    html = html.replace(/<table>/g, '<div class="table-wrapper"><table>');
    html = html.replace(/<\/table>/g, '</table></div>');
    return html;
  } catch (e) {
    return escapeHtml(content);
  }
}

function renderMath(root) {
  if (typeof renderMathInElement === 'undefined') return;
  try {
    renderMathInElement(root || document, {
      delimiters: [
        { left: '$$', right: '$$', display: true },
        { left: '$', right: '$', display: false }
      ],
      throwOnError: false,
      macros: { '\\R': '\\mathbb{R}', '\\N': '\\mathbb{N}', '\\Z': '\\mathbb{Z}' }
    });
  } catch (e) {}
}

function getOrCreateMsgGroup(forcedRhythm) {
  if (!lastGroupEl || !document.body.contains(lastGroupEl) || forcedRhythm === 'loose') {
    lastGroupEl = document.createElement('div');
    lastGroupEl.className = 'msg-group';
    if (forcedRhythm) lastGroupEl.dataset.rhythm = forcedRhythm;
    messagesEl.appendChild(lastGroupEl);
  } else if (forcedRhythm && !lastGroupEl.dataset.rhythm) {
    // 设置节奏（如果尚未设置）
    lastGroupEl.dataset.rhythm = forcedRhythm;
  }
  return lastGroupEl;
}

// ── 复制消息内容 ──
function copyMsgContent(btnEl) {
  var msg = btnEl.closest('.msg');
  if (!msg) return;
  var contentEl = msg.querySelector('.msg-content');
  if (!contentEl) return;
  var text = contentEl.textContent || contentEl.innerText || '';
  navigator.clipboard.writeText(text).catch(function() {
    // Fallback for older browsers
    var ta = document.createElement('textarea');
    ta.value = text;
    ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta);
    ta.select();
    document.execCommand('copy');
    document.body.removeChild(ta);
  });
  // 反馈动画
  btnEl.classList.add('copied');
  setTimeout(function() { btnEl.classList.remove('copied'); }, 1200);
}

// ── 点击折叠/展开 tool 消息
function toggleToolMsg(event) {
  if (event.target.closest('.backtrack-btn') || event.target.closest('.msg-avatar')) return;
  var msg = event.currentTarget;
  if (!msg.classList.contains('tool-msg')) return;
  msg.classList.toggle('collapsed');
}

function addUserMessage(content) {
  var group = getOrCreateMsgGroup('loose');
  liveMsgIndex++;
  var idx = liveMsgIndex;

  var msg = document.createElement('div');
  msg.className = 'msg user-msg';
  msg.innerHTML =
    '<div class="msg-avatar user" aria-hidden="true">👤</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author user">OPERATOR</span>' +
        '<span class="msg-time">' + formatTime(Date.now()/1000) + '</span>' +
        '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>' +
        '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
      '</div>' +
      '<div class="msg-content">' + escapeHtml(content) + '</div>' +
    '</div>';
  group.appendChild(msg);

  // 复制按钮事件
  msg.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

  // 状态栏
  _sbData.msgs++;
  updateStatusBar();

  scrollToBottom();
}

function addAssistantMessage(content) {
  var group = getOrCreateMsgGroup();
  liveMsgIndex++;
  var idx = liveMsgIndex;

  var msg = document.createElement('div');
  msg.className = 'msg assistant-msg';
  msg.innerHTML =
    '<div class="msg-avatar assistant" aria-hidden="true">' +
      '<img src="/static/favicon.png" width="18" height="19" alt="" style="vertical-align:middle">' +
    '</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author assistant">FP</span>' +
        '<span class="msg-time">' + formatTime(Date.now()/1000) + '</span>' +
        '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>' +
        '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
      '</div>' +
      '<div class="msg-content rendered"></div>' +
    '</div>';
  group.appendChild(msg);

  // 复制按钮事件
  msg.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

  var contentEl = msg.querySelector('.msg-content');
  if (content) {
    contentEl.innerHTML = renderMarkdown(content);
    renderMath();
  }

  scrollToBottom();
  return contentEl;
}

// ── LLM 工具调用记录消息 ──
// 当 LLM 只返回 tool_calls 无文本内容时，显示专用消息
function addAssistantToolCallMsg(toolNames) {
  var group = getOrCreateMsgGroup();
  liveMsgIndex++;
  var idx = liveMsgIndex;

  var msg = document.createElement('div');
  msg.className = 'msg assistant-msg assistant-toolcall-msg';
  msg.innerHTML =
    '<div class="msg-avatar assistant" aria-hidden="true">' +
      '<img src="/static/favicon.png" width="18" height="19" alt="" style="vertical-align:middle">' +
    '</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author assistant">FP</span>' +
        '<span class="msg-time">' + formatTime(Date.now()/1000) + '</span>' +
        '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>' +
        '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
      '</div>' +
      '<div class="msg-content toolcall-content">' +
        '<span class="toolcall-arrow">╰─➤</span> 调用工具: <strong>' +
        toolNames.map(function(n) { return escapeHtml(n); }).join(', ') +
        '</strong>' +
      '</div>' +
    '</div>';
  group.appendChild(msg);
  // 复制按钮事件
  msg.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };
  scrollToBottom();
}

// ── 工具卡片（tool_call_id 驱动匹配，解决并行调用同名工具错乱）──
function addToolCall(name, args, tool_call_id) {
  // 无 tool_call_id 的事件是重复源（WebSocketIO 直发），跳过
  if (!tool_call_id) return;

  // 已有同名 tool_call_id 的卡片 → 跳过重复
  if (toolCardMap[tool_call_id]) return;

  removeThinking();
  var group = getOrCreateMsgGroup('tight');

  // 状态栏
  _sbData.toolsActive++;
  _sbData.toolsTotal++;
  updateStatusBar();

  // ⚠️ 关键修复：tool 消息在后端文件中也占一个索引位置，
  // 所以 liveMsgIndex 必须递增，否则后续 user/assistant 消息的
  // 回溯按钮 data-index 会与后端 /back 索引错位（偏差 = tool 消息数）。
  liveMsgIndex++;
  var idx = liveMsgIndex;

  var prettyArgs = '';
  try {
    var parsed = typeof args === 'string' ? JSON.parse(args) : args;
    prettyArgs = JSON.stringify(parsed, null, 2);
  } catch(e) {
    prettyArgs = args || '{}';
  }

  var msg = document.createElement('div');
  msg.className = 'msg tool-msg collapsed';
  msg.dataset.toolName = name;
  msg.dataset.toolState = 'running';
  msg.dataset.toolCallId = tool_call_id;
  msg.dataset.msgIndex = idx;
  msg.onclick = toggleToolMsg;

  msg.innerHTML =
    '<div class="msg-avatar tool" aria-hidden="true">🔧</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author tool">Tool</span>' +
        '<span class="msg-time">#' + idx + '</span>' +
        '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>' +
      '</div>' +
      '<div class="msg-content rendered">' +
        '<div class="tool-msg-toggle">' +
          '<span class="tool-msg-icon">▶</span>' +
          '<span class="tool-state-icon">⏳</span>' +
          '<strong>🛠️ ' + escapeHtml(name) + '</strong>' +
          '<span class="tool-msg-status" data-state="running">运行中...</span>' +
        '</div>' +
        '<div class="tool-msg-body">' +
          '<div class="tool-args-label">参数:</div>' +
          '<pre class="tool-args-json">' + escapeHtml(prettyArgs) + '</pre>' +
        '</div>' +
      '</div>' +
    '</div>';

  group.appendChild(msg);

  // ── 注册到工具卡片映射表 ──
  toolCardMap[tool_call_id] = msg;

  scrollToBottom();
}

// ── 更新工具卡片结果（tool_call_id 精确匹配）──
function addToolResult(name, result, isError, tool_call_id) {
  // 无 tool_call_id 的事件无法精确匹配，跳过
  if (!tool_call_id) return;

  // 从映射表中查找卡片
  var target = toolCardMap[tool_call_id];

  if (!target || !document.body.contains(target)) {
    // 卡片已不存在（如清空UI后）→ 忽略
    return;
  }

  // ── 状态栏 ──
  _sbData.toolsActive = Math.max(0, _sbData.toolsActive - 1);
  updateStatusBar();

  // ── 更新状态 ──
  var newState = isError ? 'failed' : 'completed';
  target.dataset.toolState = newState;

  var stateIcon = target.querySelector('.tool-state-icon');
  if (stateIcon) {
    stateIcon.textContent = isError ? '❌' : '✅';
  }

  var statusEl = target.querySelector('.tool-msg-status');
  if (statusEl) {
    // 计算耗时
    var startTime = parseInt(target.dataset.startTime, 10);
    var elapsed = '';
    if (startTime) {
      var delta = Date.now() - startTime;
      elapsed = delta < 1000 ? ' (' + delta + 'ms)' : ' (' + (delta / 1000).toFixed(1) + 's)';
    }
    statusEl.textContent = (isError ? '失败' : '完成') + elapsed;
    statusEl.dataset.state = newState;
  }

  // ── 格式化结果 ──
  var bodyEl = target.querySelector('.tool-msg-body');
  if (bodyEl) {
    var displayResult = result || '(空)';
    // 尝试 JSON 格式化
    try {
      var parsed = typeof displayResult === 'string' ? JSON.parse(displayResult) : displayResult;
      displayResult = JSON.stringify(parsed, null, 2);
    } catch(e) { /* 保持原样 */ }

    bodyEl.innerHTML =
      '<div class="tool-result-label">' + (isError ? '错误:' : '返回:') + '</div>' +
      '<pre class="tool-result-body">' + escapeHtml(displayResult) + '</pre>';
  }

  scrollToBottom();
}

function showThinking() {
  removeThinking();
  _sbData.state = 'thinking';
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
    '<span class="thinking-label">⏳ 处理中</span>' +
    '<span class="thinking-wave" aria-label="处理中">' + bars + '</span>';
  group.appendChild(thinkingEl);

  scrollToBottom();
}

function removeThinking() {
  if (thinkingEl && document.body.contains(thinkingEl)) { thinkingEl.remove(); thinkingEl = null; }
  if (progressEl && document.body.contains(progressEl)) { progressEl.remove(); progressEl = null; }
}

function showError(msg) {
  var group = getOrCreateMsgGroup();
  var err = document.createElement('div');
  err.className = 'error-msg';
  err.setAttribute('role', 'alert');
  err.textContent = '❌ ' + msg;
  group.appendChild(err);
  scrollToBottom();
}

function setProcessing(state) {
  processing = state;
  inputEl.disabled = state;

  if (state) {
    // 处理中 → 按钮变为中断按钮 ⏹️
    _sbData.state = 'processing';
    sendBtn.disabled = false;
    sendBtn.textContent = '⏹️';
    sendBtn.style.background = '#8b3a3a';
    sendBtn.style.borderLeftColor = '#5a2a2a';
    sendBtn.onclick = cancelMessage;
    sendBtn.title = '中断 (Ctrl+Shift+C)';
    sendBtn.setAttribute('aria-label', '中断');
  } else {
    // 空闲 → 恢复为发送按钮 ➤
    _sbData.state = 'idle';
    sendBtn.disabled = false;
    sendBtn.textContent = '➤';
    sendBtn.style.background = '';
    sendBtn.style.borderLeftColor = '';
    sendBtn.onclick = sendMessage;
    sendBtn.title = '发送 (Enter)';
    sendBtn.setAttribute('aria-label', '发送消息');
    inputEl.focus();
  }
  updateStatusBar();
}

function clearUI() {
  messagesEl.innerHTML = '';
  lastGroupEl = null;
  liveMsgIndex = 0;  // 重置索引计数器，后续由 fetchSessionHistory 重新设置
  toolCardMap = {};  // 清空工具卡片映射表
  removeThinking();
  // 重置状态栏
  _sbData.toolsActive = 0;
  _sbData.toolsTotal = 0;
  _sbData.msgs = 0;
  updateStatusBar();
}

// ── WebSocket ──

var commandsCache = [];
var cmdHighlightIdx = -1;

function toggleCmdPalette() {
  var palette = document.getElementById('cmdPalette');
  if (palette.hidden) {
    palette.hidden = false;
    commandsCache = [];
    cmdHighlightIdx = -1;
    renderCmdList();
    // 从 fp-core 后端接口获取命令列表（动态，自动适配新增命令）
    authFetch('/api/commands').then(function(r) { return r.json(); }).then(function(data) {
      if (data.commands && data.commands.length > 0) {
        commandsCache = data.commands;
        renderCmdList();
      }
    }).catch(function(e) {
      console.warn('[CmdPalette] 获取命令列表失败:', e);
    });
  } else {
    closeCmdPalette();
  }
}

function closeCmdPalette() {
  document.getElementById('cmdPalette').hidden = true;
  cmdHighlightIdx = -1;
}

function renderCmdList() {
  var list = document.getElementById('cmdList');
  if (!commandsCache || commandsCache.length === 0) {
    list.innerHTML = '<div class="cmd-empty">暂无可用命令</div>';
    return;
  }

  var html = commandsCache.map(function(cmd, i) {
    var hl = i === cmdHighlightIdx ? ' highlighted' : '';
    var cmdName = cmd.name || '';
    var cmdDesc = cmd.desc || cmd.description || '';
    return '<div class="cmd-item' + hl + '" data-index="' + i + '">' +
      '<div class="cmd-item-left">' +
        '<span class="cmd-item-key">' + escapeHtml(cmdName) + '</span>' +
        '<span class="cmd-item-desc">' + escapeHtml(cmdDesc) + '</span>' +
      '</div>' +
    '</div>';
  }).join('');

  list.innerHTML = html;
  var highlighted = list.querySelector('.highlighted');
  if (highlighted) highlighted.scrollIntoView({ block: 'nearest' });
}

// 事件委托：点击命令项
document.getElementById('cmdList').addEventListener('click', function(e) {
  var item = e.target.closest('.cmd-item');
  if (!item) return;
  // 阻止冒泡到 document 的关闭处理器。
  // selectCmd() 可能调用 renderCmdList() 替换 innerHTML，
  // 导致 e.target 变成孤儿节点，closest('.input-area') 返回 null，
  // 进而被 document 处理器误判为"点击外部"关闭面板。
  e.stopPropagation();
  var index = parseInt(item.getAttribute('data-index'), 10);
  if (!isNaN(index)) selectCmd(index);
});

// 事件委托：hover 高亮（直接操作 class，不重建 DOM）
document.getElementById('cmdList').addEventListener('mouseover', function(e) {
  var item = e.target.closest('.cmd-item');
  if (!item) return;
  // 清除全部高亮
  var all = document.querySelectorAll('.cmd-item.highlighted');
  for (var j = 0; j < all.length; j++) all[j].classList.remove('highlighted');
  // 高亮当前
  item.classList.add('highlighted');
  // 同步键盘导航索引
  var items = document.querySelectorAll('.cmd-item');
  for (var k = 0; k < items.length; k++) {
    if (items[k] === item) { cmdHighlightIdx = k; break; }
  }
});
// 鼠标离开列表 → 清除高亮
document.getElementById('cmdList').addEventListener('mouseleave', function() {
  var all = document.querySelectorAll('.cmd-item.highlighted');
  for (var j = 0; j < all.length; j++) all[j].classList.remove('highlighted');
  cmdHighlightIdx = -1;
});

function selectCmd(index) {
  var cmd = commandsCache[index];
  if (!cmd) return;

  // 填入输入框
  var text = (cmd.name || '') + ' ';
  inputEl.value = text;
  autoResize(inputEl);
  var len = inputEl.value.length;
  inputEl.setSelectionRange(len, len);
  closeCmdPalette();
  inputEl.focus();
}

function navigateCmdList(direction) {
  if (!commandsCache || commandsCache.length === 0) return;
  var total = commandsCache.length;
  cmdHighlightIdx += direction;
  if (cmdHighlightIdx < 0) cmdHighlightIdx = total - 1;
  if (cmdHighlightIdx >= total) cmdHighlightIdx = 0;
  renderCmdList();
}

function confirmCmdSelection() {
  if (cmdHighlightIdx < 0) return;
  selectCmd(cmdHighlightIdx);
}

function addSystemMessage(text) {
  var group = getOrCreateMsgGroup();
  var el = document.createElement('div');
  el.className = 'sys-msg';
  el.textContent = text;
  group.appendChild(el);
  scrollToBottom();
}

// ── 会话历史 ──

var _pendingBacktrack = false;
var liveMsgIndex = 0;

function renderHistoryMessages(sid, messages) {
  var container = document.getElementById('messagesContainer');
  var sep = document.createElement('div');
  sep.className = 'history-separator';
  sep.textContent = '📜 历史记录';
  container.appendChild(sep);

  var allRendered = [];

  // 第一遍：从 assistant 消息构建 tool_call_id → tool_name 映射
  var toolNameMap = {};
  messages.forEach(function(msg) {
    if (msg.tool_calls && msg.tool_calls.length > 0) {
      msg.tool_calls.forEach(function(tc) {
        if (tc.id && tc.function && tc.function.name) {
          toolNameMap[tc.id] = tc.function.name;
        }
      });
    }
  });

  messages.forEach(function(msg) {
    var group = document.createElement('div');
    group.className = 'msg-group';
    group.dataset.rhythm = 'compact';
    group.style.borderBottom = 'none';
    group.style.padding = '1px 0';

    var msgDiv = document.createElement('div');

    // ── system 消息（如 compact 摘要）：特殊渲染，不显示回溯按钮 ──
    if (msg.role === 'system') {
      msgDiv.className = 'msg';
      msgDiv.innerHTML =
        '<div class="msg-avatar tool" aria-hidden="true">📋</div>' +
        '<div class="msg-body">' +
          '<div class="msg-header">' +
            '<span class="msg-author tool">System</span>' +
          '</div>' +
          '<div class="msg-content" style="font-size:0.85em;color:var(--text-dim);font-style:italic;white-space:pre-wrap">' +
            escapeHtml(msg.content || '') +
          '</div>' +
        '</div>';
      group.appendChild(msgDiv);
      container.appendChild(group);
      return;
    }

    if (msg.role === 'tool') {
      // ── tool 消息：可折叠，使用 data-tool-state 状态驱动 ──
      msgDiv.className = 'msg tool-msg collapsed';
      msgDiv.dataset.toolState = 'completed';
      msgDiv.dataset.toolName = toolName;
      msgDiv.onclick = toggleToolMsg;

      var toolName = toolNameMap[msg.tool_call_id] || 'Tool';
      var hasResult = msg.content && msg.content !== '';
      var displayResult = hasResult ? msg.content : '(空)';
      // 尝试 JSON 格式化
      try {
        var parsed = typeof displayResult === 'string' ? JSON.parse(displayResult) : displayResult;
        displayResult = JSON.stringify(parsed, null, 2);
      } catch(e) { /* 保持原样 */ }

      msgDiv.innerHTML =
        '<div class="msg-avatar tool" aria-hidden="true">🔧</div>' +
        '<div class="msg-body">' +
          '<div class="msg-header">' +
            '<span class="msg-author tool">Tool</span>' +
            '<span class="msg-time">#' + (msg.index || '?') + '</span>' +
            (msg.index ? '<button class="backtrack-btn" title="回溯到此位置" data-index="' + msg.index + '" aria-label="回溯到位置 ' + msg.index + '">↩ 回溯</button>' : '') +
            '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
          '</div>' +
          '<div class="msg-content rendered">' +
            '<div class="tool-msg-toggle">' +
              '<span class="tool-msg-icon">▶</span>' +
              '<span class="tool-state-icon">✅</span>' +
              '<strong>🛠️ ' + escapeHtml(toolName) + '</strong>' +
              '<span class="tool-msg-status" data-state="completed">完成</span>' +
            '</div>' +
            '<div class="tool-msg-body">' +
              '<div class="tool-result-label">返回:</div>' +
              '<pre class="tool-result-body">' + escapeHtml(displayResult) + '</pre>' +
            '</div>' +
          '</div>' +
        '</div>';

      group.appendChild(msgDiv);
      msgDiv.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

    } else if (msg.role === 'assistant' && msg.tool_calls && msg.tool_calls.length > 0 && !msg.content) {
      // ── 纯工具调用消息（无文本内容）→ 渲染 LLM 工具选择记录，跳过空白条目 ──
      msgDiv.className = 'msg assistant-msg assistant-toolcall-msg';
      var toolCallNames = msg.tool_calls.map(function(tc) {
        return (tc.function && tc.function.name) || 'Tool';
      });
      msgDiv.innerHTML =
        '<div class="msg-avatar assistant" aria-hidden="true">' +
          '<img src="/static/favicon.png" width="18" height="19" alt="" style="vertical-align:middle">' +
        '</div>' +
        '<div class="msg-body">' +
          '<div class="msg-header">' +
            '<span class="msg-author assistant">FP</span>' +
            '<span class="msg-time">#' + (msg.index || '?') + '</span>' +
            (msg.index ? '<button class="backtrack-btn" title="回溯到此位置" data-index="' + msg.index + '" aria-label="回溯到位置 ' + msg.index + '">↩ 回溯</button>' : '') +
            '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
          '</div>' +
          '<div class="msg-content toolcall-content">' +
            '<span class="toolcall-arrow">╰─➤</span> 调用工具: <strong>' +
            escapeHtml(toolCallNames.join(', ')) +
            '</strong>' +
          '</div>' +
        '</div>';
      group.appendChild(msgDiv);
      msgDiv.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

    } else {
      // ── 普通消息（user / assistant）──
      var roleSuffix = (msg.role === 'user') ? ' user-msg' : ' assistant-msg';
      msgDiv.className = 'msg' + roleSuffix;

      var roleConfig;
      if (msg.role === 'user') {
        roleConfig = { avatar: '👤', author: 'OPERATOR', cls: 'user' };
      } else {
        roleConfig = { avatar: '<img src=\"/static/favicon.png\" width=\"18\" height=\"19\" alt=\"\" style=\"vertical-align:middle\">', author: 'FP', cls: 'assistant' };
      }

      var avatarHtml = '<div class="msg-avatar ' + roleConfig.cls + '" aria-hidden="true">' + roleConfig.avatar + '</div>';

      msgDiv.innerHTML =
        avatarHtml +
        '<div class="msg-body">' +
          '<div class="msg-header">' +
            '<span class="msg-author ' + roleConfig.cls + '">' + roleConfig.author + '</span>' +
            '<span class="msg-time">#' + (msg.index || '?') + '</span>' +
            (msg.index ? '<button class="backtrack-btn" title="回溯到此位置" data-index="' + msg.index + '" aria-label="回溯到位置 ' + msg.index + '">↩ 回溯</button>' : '') +
            '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
          '</div>' +
          '<div class="msg-content rendered"></div>' +
        '</div>';

      group.appendChild(msgDiv);

      // 复制按钮事件
      var copyBtn = msgDiv.querySelector('.copy-btn');
      if (copyBtn) copyBtn.onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

      // 渲染内容
      var contentEl = msgDiv.querySelector('.msg-content');
      if (msg.content) {
        contentEl.innerHTML = renderMarkdown(msg.content);
        allRendered.push(contentEl);
      }
    }

    container.appendChild(group);
  });

  // 批量渲染公式
  if (allRendered.length > 0) {
    renderMath();
  }

  scrollToBottom();
}

/* ═══════════════════════════════════════════════════════════
   光环背景 — 弹性稳态环系统
   空闲（冷青）↔ 思考（暖橙）双色切换
   ═══════════════════════════════════════════════════════════ */

(function() {
  'use strict';

  var haloEl = document.getElementById('halo-bg');
  if (!haloEl) return;

  // ── 色板 ──
  var COLORS = {
    idle: {
      glow:   '#8fb3a6',
      sparse: '#cfe7df',
      ring:   '#bfe0d6',
      paired: '#d6ece4',
      dense:  '#c4e0d8'
    },
    thinking: {
      glow:   '#a65e2e',
      sparse: '#c47a3e',
      ring:   '#d48a4a',
      paired: '#e8a060',
      dense:  '#f0b878'
    }
  };

  // ── 浅色主题色板（在 #f4efe9 暖白底上需要更深、更饱和）──
  var COLORS_LIGHT = {
    idle: {
      glow:   '#4a7a6a',
      sparse: '#6aae98',
      ring:   '#589e88',
      paired: '#7abcac',
      dense:  '#66b4a0'
    },
    thinking: {
      glow:   '#7a3a1a',
      sparse: '#9a5a2e',
      ring:   '#aa6a3a',
      paired: '#c08050',
      dense:  '#d09060'
    }
  };

  // ring DOM 元素的类名 → 属性名映射
  var RING_CLASSES = [
    { cls: 'h-glow',   attr: 'glow'   },
    { cls: 'h-sparse', attr: 'sparse' },
    { cls: 'h-ring',   attr: 'ring'   },
    { cls: 'h-paired', attr: 'paired' },
    { cls: 'h-dense',  attr: 'dense'  }
  ];

  function isLightTheme() {
    return document.documentElement.classList.contains('light-theme');
  }

  function applyColors(state) {
    var paletteSet = isLightTheme() ? COLORS_LIGHT : COLORS;
    var palette = paletteSet[state] || paletteSet.idle;
    RING_CLASSES.forEach(function(ring) {
      var els = haloEl.querySelectorAll('.' + ring.cls);
      var color = palette[ring.attr];
      for (var i = 0; i < els.length; i++) {
        els[i].setAttribute('stroke', color);
      }
    });
    haloEl.setAttribute('data-state', state);
  }

  // ── 双色状态切换：监听 _sbData.state ──
  function syncHaloState() {
    if (!haloEl) return;
    var isThinking = (_sbData.state === 'thinking' || _sbData.state === 'processing');
    applyColors(isThinking ? 'thinking' : 'idle');
  }

  // ── 主题切换时自动刷新颜色 ──
  var themeObserver = new MutationObserver(function() {
    syncHaloState();
  });
  themeObserver.observe(document.documentElement, { attributes: true, attributeFilter: ['class'] });

  // 初始同步
  syncHaloState();

  // 拦截 setProcessing & showThinking 原始函数，确保状态同步
  var origSetProcessing = window.setProcessing;
  window.setProcessing = function(state) {
    if (origSetProcessing) origSetProcessing(state);
    syncHaloState();
  };

  var origShowThinking = window.showThinking;
  window.showThinking = function() {
    if (origShowThinking) origShowThinking();
    syncHaloState();
  };

  var origRemoveThinking = window.removeThinking;
  window.removeThinking = function() {
    if (origRemoveThinking) origRemoveThinking();
    requestAnimationFrame(syncHaloState);
  };

  const GAP = 3;
  const MIN_THICKNESS = 1.5;
  const MAX_R = 195;

  const layers = [
    { id:'l2', iInt:2200, oInt:3100, tDur:600 },
    { id:'l3', iInt:2800, oInt:3700, tDur:500 },
    { id:'l4', iInt:3400, oInt:4300, tDur:450 },
    { id:'l5', iInt:4100, oInt:5200, tDur:550 },
    { id:'l6', iInt:4800, oInt:6100, tDur:500 },
  ];

  const els = layers.map(l => document.getElementById(l.id));

  // 初始位置（只决定了开局，之后系统自由漂移）
  const initBounds = [
    { i: 20, o: 80 }, { i: 115, o: 125 }, { i: 136, o: 138 },
    { i: 150, o: 162 }, { i: 175, o: 185 }
  ];

  const state = layers.map((l, idx) => ({
    inner: {
      current: initBounds[idx].i, target: initBounds[idx].i, from: initBounds[idx].i,
      startTime: 0, dur: l.tDur, nextPulse: 0, interval: l.iInt, tDur: l.tDur
    },
    outer: {
      current: initBounds[idx].o, target: initBounds[idx].o, from: initBounds[idx].o,
      startTime: 0, dur: l.tDur, nextPulse: 0, interval: l.oInt, tDur: l.tDur
    },
  }));

  function easeOutCubic(t) { return 1 - Math.pow(1 - t, 3); }
  function nextPulseTime(now, avgInterval) {
    return now + avgInterval * (0.3 + Math.random() * 2.2);
  }

  const now0 = performance.now();
  state.forEach(s => {
    s.inner.nextPulse = now0 + Math.random() * s.inner.interval;
    s.outer.nextPulse = now0 + Math.random() * s.outer.interval;
  });

  function updateChannel(ch, now, layerIndex, channelType) {
    if (now >= ch.nextPulse) {
      ch.from = ch.current;

      // 根据所有环的实时位置计算合法范围
      const bounds = state.map(s => ({ i: s.inner.current, o: s.outer.current }));
      let dynMin, dynMax;

      if (channelType === 'inner') {
        dynMin = (layerIndex === 0) ? 0 : bounds[layerIndex - 1].o + GAP;
        const maxBySelf = bounds[layerIndex].o - MIN_THICKNESS;
        const maxByOuter = (layerIndex === 4) ? MAX_R - MIN_THICKNESS : bounds[layerIndex + 1].i - GAP - MIN_THICKNESS;
        dynMax = Math.min(maxBySelf, maxByOuter);
      } else {
        dynMin = bounds[layerIndex].i + MIN_THICKNESS;
        dynMax = (layerIndex === 4) ? MAX_R : bounds[layerIndex + 1].i - GAP;
      }

      // 空间被挤压到连最小厚度都放不下 → 跳过本次脉冲
      if (dynMin > dynMax) {
        ch.nextPulse = nextPulseTime(now, ch.interval);
        return;
      }

      // ★ 完全随机，无 rest 偏向 —— 长时运行会探索所有可能态
      ch.target = dynMin + Math.random() * (dynMax - dynMin);
      ch.startTime = now;
      ch.dur = ch.tDur * (0.6 + Math.random() * 0.8);
      ch.nextPulse = nextPulseTime(now + ch.dur, ch.interval);
    }

    if (now < ch.startTime + ch.dur) {
      const progress = (now - ch.startTime) / ch.dur;
      const eased = easeOutCubic(Math.min(progress, 1));
      ch.current = ch.from + (ch.target - ch.from) * eased;
    } else {
      ch.current = ch.target;
    }
  }

  function tick(now) {
    for (let i = 0; i < state.length; i++) {
      updateChannel(state[i].inner, now, i, 'inner');
      updateChannel(state[i].outer, now, i, 'outer');
    }

    // 约束传播：防止缓动过渡中穿模
    for (let iter = 0; iter < 3; iter++) {
      for (let i = 0; i < state.length; i++) {
        if (i === 0) state[i].inner.current = Math.max(0, state[i].inner.current);
        else state[i].inner.current = Math.max(state[i-1].outer.current + GAP, state[i].inner.current);

        if (i === state.length - 1) state[i].outer.current = Math.min(MAX_R, state[i].outer.current);
        else state[i].outer.current = Math.min(state[i+1].inner.current - GAP, state[i].outer.current);

        if (state[i].outer.current < state[i].inner.current + MIN_THICKNESS) {
          if (i === state.length - 1 || state[i].outer.current + MIN_THICKNESS <= state[i+1].inner.current - GAP) {
            state[i].outer.current = state[i].inner.current + MIN_THICKNESS;
          } else {
            state[i].inner.current = state[i].outer.current - MIN_THICKNESS;
          }
        }
      }
    }

    for (let i = 0; i < state.length; i++) {
      const cInner = state[i].inner.current;
      const cOuter = state[i].outer.current;
      if (els[i]) {
        els[i].setAttribute('r', ((cInner + cOuter) / 2).toFixed(2));
        els[i].setAttribute('stroke-width', Math.max(cOuter - cInner, 0.1).toFixed(2));
      }
    }

    requestAnimationFrame(tick);
  }

  requestAnimationFrame(tick);
})();

