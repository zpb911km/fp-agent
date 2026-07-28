/* ═══════════════════════════════════════════════════════════
   Five Pebbles WebUI — UI 渲染层
   从 index.html 拆分，2026-07-28
   包含: 共享状态 / DOM 渲染 / 命令面板 / 消息构建
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

/* ═══════════════════════════════════════════════════════════
   Five Pebbles WebUI — 前端逻辑
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
        '<span class="msg-author assistant">FIVE PEBBLES</span>' +
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
        '<span class="msg-author assistant">FIVE PEBBLES</span>' +
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

var FALLBACK_COMMANDS = [
  { name: '/help',    desc: '显示帮助信息' },
  { name: '/new',     desc: '新建空白会话' },
  { name: '/clear',   desc: '清空当前会话' },
  { name: '/session', desc: '显示当前会话信息' },
];

var commandsCache = [];
var cmdHighlightIdx = -1;
var cmdMenuStack = [];  // [] = 顶层, [{parentName, subcommands}] = 二级菜单

function toggleCmdPalette() {
  var palette = document.getElementById('cmdPalette');
  if (palette.hidden) {
    palette.hidden = false;
    cmdMenuStack = [];
    commandsCache = FALLBACK_COMMANDS.slice();
    cmdHighlightIdx = -1;
    renderCmdList();
    // 1. 优先尝试加载 commands.json（多级命令菜单）
    fetch('/static/commands.json').then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    }).then(function(data) {
      if (Array.isArray(data) && data.length > 0) {
        commandsCache = data;
        renderCmdList();
      }
    }).catch(function() {
      // 2. 失败则降级到 /api/commands（单级扁平列表）
      authFetch('/api/commands').then(function(r) { return r.json(); }).then(function(data) {
        if (data.commands && data.commands.length > 0) {
          commandsCache = data.commands;
          renderCmdList();
        }
      }).catch(function() {});
    });
  } else {
    closeCmdPalette();
  }
}

function closeCmdPalette() {
  document.getElementById('cmdPalette').hidden = true;
  cmdHighlightIdx = -1;
  cmdMenuStack = [];
}

function cmdMenuBack() {
  if (cmdMenuStack.length === 0) return;
  cmdMenuStack.pop();
  cmdHighlightIdx = -1;
  renderCmdList();
}

function renderCmdList() {
  var list = document.getElementById('cmdList');
  if (!commandsCache || commandsCache.length === 0) {
    list.innerHTML = '<div class="cmd-empty">暂无可用命令</div>';
    return;
  }

  var items, parentName;
  if (cmdMenuStack.length > 0) {
    var state = cmdMenuStack[cmdMenuStack.length - 1];
    items = state.subcommands;
    parentName = state.parentName;
  } else {
    items = commandsCache;
    parentName = null;
  }

  var html = '';
  var offset = 0;

  // 二级菜单：顶部返回项（highlightIdx = 0）
  if (parentName) {
    html += '<div class="cmd-item cmd-back' + (cmdHighlightIdx === 0 ? ' highlighted' : '') + '">' +
      '<div class="cmd-item-left">' +
        '<span class="cmd-back-arrow">←</span>' +
        '<span class="cmd-back-label">' + escapeHtml(parentName) + '…</span>' +
      '</div>' +
    '</div>';
    offset = 1;
  }

  html += items.map(function(cmd, i) {
    var hl = (i + offset) === cmdHighlightIdx ? ' highlighted' : '';
    var cmdName = cmd.name || '';
    var cmdDesc = cmd.desc || cmd.description || '';
    var hasSub = cmd.subcommands && cmd.subcommands.length > 0 && !parentName;
    return '<div class="cmd-item' + hl + '" data-index="' + i + '">' +
      '<div class="cmd-item-left">' +
        '<span class="cmd-item-key">' + escapeHtml(cmdName) + '</span>' +
        '<span class="cmd-item-desc">' + escapeHtml(cmdDesc) + '</span>' +
      '</div>' +
      (hasSub ? '<span class="cmd-item-sub-indicator">›</span>' : '') +
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
  // 返回按钮
  if (item.classList.contains('cmd-back')) {
    cmdMenuBack();
    return;
  }
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
  var items;
  if (cmdMenuStack.length > 0) {
    items = cmdMenuStack[cmdMenuStack.length - 1].subcommands;
  } else {
    items = commandsCache;
  }
  var cmd = items[index];
  if (!cmd) return;

  // 顶层且有子命令 → 进入二级菜单
  if (cmdMenuStack.length === 0 && cmd.subcommands && cmd.subcommands.length > 0) {
    cmdMenuStack.push({
      parentName: cmd.name,
      subcommands: cmd.subcommands
    });
    cmdHighlightIdx = -1;
    renderCmdList();
    return;
  }

  // 普通命令/子命令 → 补全到输入框
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
  var total;
  if (cmdMenuStack.length > 0) {
    total = 1 + cmdMenuStack[cmdMenuStack.length - 1].subcommands.length;
  } else {
    total = commandsCache.length;
  }
  cmdHighlightIdx += direction;
  if (cmdHighlightIdx < 0) cmdHighlightIdx = total - 1;
  if (cmdHighlightIdx >= total) cmdHighlightIdx = 0;
  renderCmdList();
}

function confirmCmdSelection() {
  if (cmdHighlightIdx < 0) return;
  // 二级菜单 highlightIdx = 0 → 返回
  if (cmdMenuStack.length > 0 && cmdHighlightIdx === 0) {
    cmdMenuBack();
    return;
  }
  var actualIndex = cmdMenuStack.length > 0 ? cmdHighlightIdx - 1 : cmdHighlightIdx;
  selectCmd(actualIndex);
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
            '<span class="msg-author assistant">FIVE PEBBLES</span>' +
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
        roleConfig = { avatar: '<img src=\"/static/favicon.png\" width=\"18\" height=\"19\" alt=\"\" style=\"vertical-align:middle\">', author: 'FIVE PEBBLES', cls: 'assistant' };
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

