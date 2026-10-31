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

// ── 当前轮工具行宿主：助手卡片的 .msg-body ──
// 工具行内嵌于助手卡片（方案C），不再各自成卡。
// 纯 tool_calls 轮（无正文）懒建空卡片承载。
var _toolContainerEl = null;

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

// ── 跟随式滚底 ──
// 只有当用户本就贴近底部时才自动跟随（距底 <80px 视为贴底）；
// 上滑阅读历史即解除跟随，下滑回贴底部自动恢复。
// 「视图重置」类场景（加载历史/切会话/回溯/发送消息/启动）传 force=true 强制滚。
var FOLLOW_THRESHOLD_PX = 80;
var _followBottom = true;

function _isNearBottom() {
  return messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight < FOLLOW_THRESHOLD_PX;
}

messagesEl.addEventListener('scroll', function() {
  _followBottom = _isNearBottom();
});

function scrollToBottom(force) {
  if (!force && !_followBottom) return;
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
    // mermaid 代码块 → 图形容器（交由 mermaid.js 渲染；src 已由 marked 转义，安全）
    html = html.replace(
      /<pre><code class="language-mermaid">([\s\S]*?)<\/code><\/pre>/g,
      function (_, src) { return '<div class="mermaid">' + src + '</div>'; }
    );
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
// execCommand 降级通道：非安全上下文（http://LAN-IP 访问时
// navigator.clipboard 为 undefined）与旧浏览器都走这里
function legacyCopyText(text) {
  var ta = document.createElement('textarea');
  ta.value = text;
  ta.setAttribute('readonly', '');
  ta.style.position = 'fixed';
  ta.style.top = '0';
  ta.style.left = '-9999px';
  document.body.appendChild(ta);
  ta.select();
  try { ta.setSelectionRange(0, text.length); } catch (e) {}
  var ok = false;
  try { ok = document.execCommand('copy'); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  return ok;
}

function copyMsgContent(btnEl) {
  var msg = btnEl.closest('.msg');
  if (!msg) return;
  var contentEl = msg.querySelector('.msg-content');
  if (!contentEl) return;
  var text = contentEl.textContent || contentEl.innerText || '';

  function feedback(ok) {
    if (ok) {
      btnEl.classList.add('copied');
      setTimeout(function() { btnEl.classList.remove('copied'); }, 1200);
    } else {
      // 两条通道都失败：短暂警示，避免"看起来复制成功了"
      var old = btnEl.textContent;
      btnEl.textContent = '⚠';
      setTimeout(function() { btnEl.textContent = old; }, 1200);
    }
  }

  // 不能无条件调用 navigator.clipboard.writeText：
  // 非安全上下步下该 API 为 undefined，会同步抛 TypeError，
  // 导致 .catch 里的降级逻辑根本挂不上（原 bug）
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(
      function() { feedback(true); },
      function() { feedback(legacyCopyText(text)); }
    );
  } else {
    feedback(legacyCopyText(text));
  }
}

// ── 点击折叠/展开内嵌工具行
function toggleToolRow(event) {
  if (event.target.closest('.backtrack-btn')) return;
  var row = event.currentTarget;
  if (!row.classList.contains('tool-row')) return;
  row.classList.toggle('collapsed');
}

/**
 * 用户消息气泡。
 * @param {string} content 消息文本
 * @param {boolean} [ephemeral] true = 命令回显等不落盘消息：
 *   不计入回溯索引（liveMsgIndex）、不渲染回溯按钮（历史里没有这个位置）
 */
function addUserMessage(content, ephemeral) {
  var group = getOrCreateMsgGroup('loose');
  if (!ephemeral) { liveMsgIndex++; }
  var idx = liveMsgIndex;

  var msg = document.createElement('div');
  msg.className = 'msg user-msg';
  msg.innerHTML =
    '<div class="msg-side">' +
      '<div class="msg-avatar user" aria-hidden="true">👤</div>' +
      (ephemeral ? '' : '<div class="msg-index-badge">#' + idx + '</div>') +
    '</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author user">OPERATOR</span>' +
        '<span class="msg-time">' + formatTime(Date.now()/1000) + '</span>' +
        (ephemeral ? '' :
          '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>') +
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

  scrollToBottom(true);  // 用户主动发送 → 强制滚底展示自己的消息
  return msg;
}

// ── 排队气泡对账（queued 广播入列 → user_message 到达消费转正，FIFO 头匹配）──
// 消息经 submit 入队后，消费落盘时 ON_CTX_APPEND 会再发 user_message；
// 各设备凭此表把已渲染的 ⏳ 气泡就地转正，防同一消息渲染两次。
var _queuedPending = [];

/** 运行中排队消息：先按 ephemeral 渲染（排队内容可能是命令——不落盘、不占号），
 *  消费时经 user_message.ephemeral 判定真实身份再决定是否转正 */
function addQueuedMessage(content) {
  var msg = addUserMessage(content, true);
  if (msg) {
    var flag = document.createElement('span');
    flag.className = 'queued-flag';
    flag.textContent = ' ⏳ 已排队';
    var body = msg.querySelector('.msg-content');
    if (body) body.appendChild(flag);
    _queuedPending.push({ content: content, el: msg });
  }
  return msg;
}

/** 排队气泡转正：真实消息消费时补占索引位 + 徽章 + 回溯按钮 */
function promoteQueuedBubble(msgEl) {
  liveMsgIndex++;
  var idx = liveMsgIndex;
  var side = msgEl.querySelector('.msg-side');
  if (side) {
    var badge = document.createElement('div');
    badge.className = 'msg-index-badge';
    badge.textContent = '#' + idx;
    side.appendChild(badge);
  }
  var header = msgEl.querySelector('.msg-header');
  var copyBtn = header && header.querySelector('.copy-btn');
  if (header && copyBtn) {
    var btn = document.createElement('button');
    btn.className = 'backtrack-btn';
    btn.title = '回溯到此位置';
    btn.dataset.index = String(idx);
    btn.setAttribute('aria-label', '回溯到位置 ' + idx);
    btn.textContent = '↩ 回溯';
    header.insertBefore(btn, copyBtn);
  }
}

/**
 * user_message 到达时尝试把排队气泡就地转正（摘除 ⏳ 标记、不新建气泡）。
 * @param {string} content 事件消息文本
 * @param {boolean} [ephemeral] 命令回显 = true：气泡保持无索引无按钮
 * @returns {boolean} true = 命中队首排队气泡已转正；false = 应新建气泡
 */
function consumeQueuedBubble(content, ephemeral) {
  if (_queuedPending.length && _queuedPending[0].content === content) {
    var q = _queuedPending.shift();
    if (q.el) {
      var flag = q.el.querySelector('.queued-flag');
      if (flag) flag.remove();
      if (!ephemeral) promoteQueuedBubble(q.el);
    }
    return true;
  }
  return false;
}

function clearQueuedFlags() {
  document.querySelectorAll('.queued-flag').forEach(function (el) { el.remove(); });
}

/**
 * 助手消息卡片。
 * @param {string} content 消息文本
 * @param {boolean} [ephemeral] true = 命令输出等不落盘消息：
 *   不计入回溯索引（liveMsgIndex）、不渲染回溯按钮/徽章、不登记为工具行宿主
 */
function addAssistantMessage(content, ephemeral) {
  var group = getOrCreateMsgGroup();
  if (!ephemeral) { liveMsgIndex++; }
  var idx = liveMsgIndex;

  var msg = document.createElement('div');
  msg.className = 'msg assistant-msg';
  msg.innerHTML =
    '<div class="msg-side">' +
      '<div class="msg-avatar assistant" aria-hidden="true">' +
        '<img src="/static/favicon.png" width="18" height="19" alt="" style="vertical-align:middle">' +
      '</div>' +
      (ephemeral ? '' : '<div class="msg-index-badge">#' + idx + '</div>') +
    '</div>' +
    '<div class="msg-body">' +
      '<div class="msg-header">' +
        '<span class="msg-author assistant">FP</span>' +
        '<span class="msg-time">' + formatTime(Date.now()/1000) + '</span>' +
        (ephemeral ? '' :
          '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>') +
        '<button class="copy-btn" title="复制消息内容" aria-label="复制消息内容">📋</button>' +
      '</div>' +
      '<div class="msg-content rendered"></div>' +
    '</div>';
  group.appendChild(msg);

  // 复制按钮事件
  msg.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

  // 新助手卡片 = 本轮工具行的新宿主（ephemeral 命令卡不登记：
  // 命令轮无工具调用，若被后续轮的 ensureToolContainer 复用会导致工具行错挂）
  if (!ephemeral) {
    _toolContainerEl = msg.querySelector('.msg-body');
  }

  var contentEl = msg.querySelector('.msg-content');
  if (content) {
    contentEl.innerHTML = renderMarkdown(content);
    renderMath();
  }

  scrollToBottom();
  return contentEl;
}

// ── 工具行宿主容器 ──
// 方案C：紫卡已移除。tool_select 不再建卡，只确保当前轮有承载工具行的
// 助手卡片。有正文的轮次复用正文卡片；纯 tool_calls 轮懒建空卡片
//（空 .msg-content 挂 stream-pending 隐藏，只留 header + 工具行）。
function ensureToolContainer() {
  if (_toolContainerEl && document.body.contains(_toolContainerEl)) return _toolContainerEl;
  var contentEl = addAssistantMessage('');
  contentEl.classList.add('stream-pending');
  return _toolContainerEl; // addAssistantMessage 内部已更新
}

// ── 工具行（tool_call_id 驱动匹配，解决并行调用同名工具错乱）──
// 方案C：降级为嵌在助手卡片内的紧凑行（一轮 = 一张卡）。
// 运行中展开（实时看参数），完成/失败自动折叠为单行，点击行可再展开。
function addToolCall(name, args, tool_call_id) {
  // 无 tool_call_id 的事件是重复源（WebSocketIO 直发），跳过
  if (!tool_call_id) return;

  // 已有同名 tool_call_id 的行 → 跳过重复
  if (toolCardMap[tool_call_id]) return;

  removeThinking();

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
  // 折叠态单行的参数预览：紧凑 JSON 截断
  var argsPreview = prettyArgs.replace(/\s+/g, ' ').trim();
  if (argsPreview.length > 80) argsPreview = argsPreview.slice(0, 80) + '…';

  var container = ensureToolContainer();

  var row = document.createElement('div');
  row.className = 'tool-row';
  row.dataset.toolName = name;
  row.dataset.toolState = 'running';
  row.dataset.toolCallId = tool_call_id;
  row.dataset.msgIndex = idx;
  row.dataset.startTime = String(Date.now());
  row.onclick = toggleToolRow;

  row.innerHTML =
    '<div class="tool-row-head">' +
      '<span class="tool-row-icon">▸</span>' +
      '<span class="tool-state-icon">⏳</span>' +
      '<strong class="tool-row-name">🛠️ ' + escapeHtml(name) + '</strong>' +
      '<span class="tool-row-args-preview">' + escapeHtml(argsPreview) + '</span>' +
      '<span class="tool-row-status" data-state="running">运行中…</span>' +
    '</div>' +
    '<div class="tool-row-body">' +
      '<div class="tool-row-meta">' +
        '<span class="msg-index-badge standalone">#' + idx + '</span>' +
        '<button class="backtrack-btn" title="回溯到此位置" data-index="' + idx + '" aria-label="回溯到位置 ' + idx + '">↩ 回溯</button>' +
      '</div>' +
      '<div class="tool-args-block">' +
        '<div class="tool-args-label">参数:</div>' +
        '<pre class="tool-args-json">' + escapeHtml(prettyArgs) + '</pre>' +
      '</div>' +
    '</div>';

  container.appendChild(row);

  // ── 注册到映射表 ──
  toolCardMap[tool_call_id] = row;

  scrollToBottom();
}

// ── 更新工具行结果（tool_call_id 精确匹配）──
function addToolResult(name, result, isError, tool_call_id) {
  // 无 tool_call_id 的事件无法精确匹配，跳过
  if (!tool_call_id) return;

  // 从映射表中查找行
  var target = toolCardMap[tool_call_id];

  if (!target || !document.body.contains(target)) {
    // 行已不存在（如清空UI后）→ 忽略
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

  var statusEl = target.querySelector('.tool-row-status');
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

  // ── 格式化结果，**追加**在参数块之后（参数保留，
  //    展开即可对照「什么输入带来什么输出」）──
  var bodyEl = target.querySelector('.tool-row-body');
  if (bodyEl) {
    var displayResult = result || '(空)';
    // 尝试 JSON 格式化
    try {
      var parsed = typeof displayResult === 'string' ? JSON.parse(displayResult) : displayResult;
      displayResult = JSON.stringify(parsed, null, 2);
    } catch(e) { /* 保持原样 */ }

    var oldBlock = bodyEl.querySelector('.tool-result-block');
    if (oldBlock) oldBlock.remove();  // 重复 tool_result 事件 → 幂等
    var resBlock = document.createElement('div');
    resBlock.className = 'tool-result-block';
    resBlock.innerHTML =
      '<div class="tool-result-label">' + (isError ? '错误:' : '返回:') + '</div>' +
      '<pre class="tool-result-body">' + escapeHtml(displayResult) + '</pre>';
    bodyEl.appendChild(resBlock);
  }

  // ── 完成即折叠为单行（方案C：运行中展开，结束后让出历史高度）──
  target.classList.add('collapsed');

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
    '<span class="thinking-label">处理中</span>' +
    '<span class="thinking-wave" aria-label="处理中">' + bars + '</span>';
  group.appendChild(thinkingEl);

  scrollToBottom();
}

function removeThinking() {
  if (thinkingEl && document.body.contains(thinkingEl)) { thinkingEl.remove(); thinkingEl = null; }
  if (progressEl && document.body.contains(progressEl)) { progressEl.remove(); progressEl = null; }
}

/* ═══════════════════════════════════════════════════════════
   流式显示 — 思考块 + 正文流式（n3）
   生命周期（一轮 LLM 调用 = llm_start → … → llm_end）：
     llm_start        → startStreamRound()（重置状态，spinner 由 showThinking 管）
     thinking_content → 思考块懒建（展开态）+ 文本累积，50ms 节流 flush
     chunk            → 首块：折叠思考块；正文消息懒建，100ms 节流重渲染 markdown
     stream_end       → flush（后端已先冲刷缓冲再发，顺序安全）
     llm_end/done/…   → finalizeStreamRound()：终渲染 + 清理
   折叠策略：正文到达前思考块展开；首个正文 chunk 到达即折叠为
   「思考 N 字 · Xs」单行（点击可再展开，保留全文）。
   ═══════════════════════════════════════════════════════════ */
var _stream = null;
// 轮次序号：每轮终结（done/cancelled/error）推进一次，流式轮创建时打上当时的
// 序号（_stream.turnSeq）。_stream 终轮**不销毁**（迟到 token 要并入本轮元素），
// 于是它天然会活过轮次边界——没有序号就无法区分「本轮卡片」与「N 轮前的卡片」。
var _turnSeq = 0;
var THINK_FLUSH_MS = 50;    // 思考文本 DOM flush 节流
var CONTENT_FLUSH_MS = 100; // 正文 markdown 重渲染节流

/** 终结当前轮：此后新建的流式轮才算「下一轮」，旧轮卡片不再被终渲染认领 */
function endStreamTurn() { _turnSeq++; }

/** 现存流式轮是否属于当前这一轮（命令轮没有 llm_start，会撞上上一轮的残留） */
function isCurrentTurnRound() { return !!_stream && _stream.turnSeq === _turnSeq; }

function startStreamRound() {
  finalizeStreamRound(); // 上一轮残留兜底（flush + 终渲染）
  _toolContainerEl = null; // 新一轮：工具行宿主重置，不复用上一轮卡片
  _stream = {
    turnSeq: _turnSeq,
    thinkText: '', thinkChars: 0, thinkEl: null, thinkTimer: null,
    contentText: '', contentEl: null, contentTimer: null,
    closed: false  // llm_end/done 后置 true：迟到 token 并入本轮，不新建块
  };
}

/** 折叠思考块为单行摘要（保留全文可展开） */
function foldThinkEl(el, chars, elapsedMs) {
  if (!el || el.classList.contains('collapsed')) return;
  var statusEl = el.querySelector('.think-msg-status');
  if (statusEl) {
    statusEl.textContent = '思考 ' + chars + ' 字 · ' + (elapsedMs / 1000).toFixed(1) + 's';
  }
  el.classList.add('collapsed');
}

function toggleThinkMsg(event) {
  if (event.target.closest('.backtrack-btn') || event.target.closest('.msg-avatar')) return;
  event.currentTarget.classList.toggle('collapsed');
}

/** 构建思考块元素（流式与历史回看共用） */
function buildThinkBlock(text) {
  var el = document.createElement('div');
  el.className = 'think-msg';
  el.innerHTML =
    '<div class="think-msg-toggle">' +
      '<span class="think-msg-icon">▸</span>' +
      '<span class="think-msg-label">思考中…</span>' +
      '<span class="think-msg-status"></span>' +
    '</div>' +
    '<div class="think-msg-body"></div>';
  el.querySelector('.think-msg-body').textContent = text;
  el.onclick = toggleThinkMsg;
  return el;
}

function streamThinkToken(token) {
  if (!_stream) startStreamRound();
  if (!_stream.thinkEl) {
    // 首个思考 token：spinner 让位给真内容
    removeThinking();
    // 回复卡片先行创建，思考块插在卡片内部（header 与 content 之间），
    // 与历史回看路径同构——不再裸挂 msg-group 下破坏卡片统一性
    if (!_stream.contentEl) _stream.contentEl = addAssistantMessage('');
    // 正文未到：隐藏空的 .msg-content，避免思考期卡片下方出现空白占位
    _stream.contentEl.classList.add('stream-pending');
    _stream.thinkEl = buildThinkBlock('');
    _stream.thinkEl.dataset.streamStart = String(Date.now());
    _stream.contentEl.parentNode.insertBefore(_stream.thinkEl, _stream.contentEl);
    scrollToBottom();
  }
  _stream.thinkText += token;
  _stream.thinkChars += token.length;
  if (_stream.closed) {
    // 迟到 token（llm_end 后 fire-and-forget 排序晚到）：直写终帧，不排定时器
    if (_stream.thinkTimer) { clearTimeout(_stream.thinkTimer); _stream.thinkTimer = null; }
    var lateBody = _stream.thinkEl.querySelector('.think-msg-body');
    lateBody.textContent = _stream.thinkText;
    lateBody.scrollTop = lateBody.scrollHeight; // 内部滚动区（40vh 封顶）跟随到底
    return;
  }
  if (_stream.thinkTimer) return;
  _stream.thinkTimer = setTimeout(function() {
    if (!_stream || !_stream.thinkEl) return;
    _stream.thinkTimer = null;
    var body = _stream.thinkEl.querySelector('.think-msg-body');
    body.textContent = _stream.thinkText;
    body.scrollTop = body.scrollHeight; // 内部滚动区跟随到底，最新思考始终可见
    scrollToBottom();
  }, THINK_FLUSH_MS);
}

function streamContentChunk(chunk) {
  if (!_stream) startStreamRound();
  // 正文到达 → 思考块折叠（用户决策：正文前展开，正文流式时折叠）
  if (_stream.thinkEl) {
    var t0 = Number(_stream.thinkEl.dataset.streamStart || Date.now());
    foldThinkEl(_stream.thinkEl, _stream.thinkChars, Date.now() - t0);
  }
  if (!_stream.contentEl) {
    // 复用 addAssistantMessage 的完整消息结构（头像/时间戳/回溯/复制 + liveMsgIndex 记账）
    _stream.contentEl = addAssistantMessage('');
  }
  // 正文首块到达 → 取消空占位隐藏
  _stream.contentEl.classList.remove('stream-pending');
  _stream.contentText += chunk;
  if (_stream.closed) {
    // 迟到 chunk：直写终帧，不排定时器
    if (_stream.contentTimer) { clearTimeout(_stream.contentTimer); _stream.contentTimer = null; }
    _stream.contentEl.innerHTML = renderMarkdown(_stream.contentText);
    return;
  }
  if (_stream.contentTimer) return;
  _stream.contentTimer = setTimeout(function() {
    if (!_stream || !_stream.contentEl) return;
    _stream.contentTimer = null;
    _stream.contentEl.innerHTML = renderMarkdown(_stream.contentText);
    scrollToBottom();
  }, CONTENT_FLUSH_MS);
}

/** 终渲染：flush 残余 + 用权威全文重渲染。
    轮次置 closed 但不销毁——迟到 token（后端 _pub fire-and-forget 排在
    llm_end 之后的末批）并入本轮元素，避免开孤儿思考块。返回是否已显示正文 */
function finalizeStreamRound(finalContent) {
  if (!_stream) return false;
  var s = _stream;
  s.closed = true;
  if (s.thinkTimer) { clearTimeout(s.thinkTimer); s.thinkTimer = null; }
  if (s.contentTimer) { clearTimeout(s.contentTimer); s.contentTimer = null; }
  if (s.thinkEl) {
    // 未被正文折叠（如纯思考后中断/出错）→ 保留展开态最后一帧
    s.thinkEl.querySelector('.think-msg-body').textContent = s.thinkText;
  }
  // 卡片可能已随 clearUI 作废（重连 resync / 回溯重拉）：此时再写就是写进
  // 脱离文档的"幽灵卡"——写成功了、用户什么也看不到。判为未显示，交回调用方重渲染。
  var shown = !!s.contentEl && document.body.contains(s.contentEl);
  if (shown && finalContent != null) {
    // 权威全文覆盖（llm_end.content / done.final_content）
    s.contentEl.classList.remove('stream-pending');
    s.contentEl.innerHTML = renderMarkdown(finalContent);
    renderMath();
  }
  scrollToBottom();
  return shown;
}

/** done 专用终渲染：只承接**本轮**的流式卡片，并推进轮次序号。

    命令轮（/sc、/task、/memory…）不走 llm_start → startStreamRound 不重置
    _stream，它仍指向上一轮的卡片（终轮不销毁，见上）。若 done 照旧把
    `final_content` 写进去，命令输出的文本就会**覆盖上一轮的正文**：
    卡片看似"被吞掉"、信息缺失，而刷新页面走 REST 历史又完好无损
    （典型症状：/sc 的结果出现在上一轮答案的位置、命令气泡反而在它下面）。
    故：跨轮不写，交回调用方 addAssistantMessage 另起卡片。

    返回 true 表示「本轮正文已由流式卡片承载」（调用方跳过重复渲染）。*/
function finalizeStreamRoundForDone(finalContent) {
  var sameTurn = isCurrentTurnRound();
  var shown = sameTurn ? finalizeStreamRound(finalContent) : false;
  endStreamTurn();  // 本轮到此终结：下一轮的卡片必须换序号
  return shown;
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
  // 运行中输入框保持可用（#158 排队发送），仅在结束时清除 ⏳ 标记
  if (!state) { clearQueuedFlags(); }

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
  _toolContainerEl = null;  // 工具行宿主随卡片一起作废
  _queuedPending = [];  // 排队对账表随气泡一起作废（重拉/回溯后从新渲染）
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

  // 最近一张助手卡片的 msg-body：后续 tool 行的内嵌宿主
  var _histToolHost = null;

  // 第一遍：从 assistant 消息构建 tool_call_id → tool_name / tool_args 映射
  //（tool 消息本身只有返回值；调用参数存在对应 assistant.tool_calls 里）
  var toolNameMap = {};
  var toolArgsMap = {};
  messages.forEach(function(msg) {
    if (msg.tool_calls && msg.tool_calls.length > 0) {
      msg.tool_calls.forEach(function(tc) {
        if (tc.id && tc.function && tc.function.name) {
          toolNameMap[tc.id] = tc.function.name;
          var rawArgs = tc.function.arguments;
          if (rawArgs && rawArgs !== '{}' && rawArgs !== '') {
            try {
              toolArgsMap[tc.id] = JSON.stringify(
                typeof rawArgs === 'string' ? JSON.parse(rawArgs) : rawArgs, null, 2);
            } catch (e) {
              toolArgsMap[tc.id] = typeof rawArgs === 'string' ? rawArgs : JSON.stringify(rawArgs, null, 2);
            }
          }
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
      // ── tool 消息：紧凑行，内嵌前一张助手卡片（方案C，与实时渲染同构）──
      var toolName = toolNameMap[msg.tool_call_id] || 'Tool';

      var hasResult = msg.content && msg.content !== '';
      var displayResult = hasResult ? msg.content : '(空)';
      // 尝试 JSON 格式化
      try {
        var parsed = typeof displayResult === 'string' ? JSON.parse(displayResult) : displayResult;
        displayResult = JSON.stringify(parsed, null, 2);
      } catch(e) { /* 保持原样 */ }

      var histArgs = toolArgsMap[msg.tool_call_id] || '';
      var argsPreview = histArgs ? histArgs.replace(/\s+/g, ' ').trim() : '';
      if (argsPreview.length > 80) argsPreview = argsPreview.slice(0, 80) + '…';

      // 宿主：前一张助手卡片的 msg-body；缺失（历史截断）→ 兜底独立成组
      var hostBody = _histToolHost;
      if (!hostBody || !document.body.contains(hostBody)) {
        hostBody = null;
      }

      var row = document.createElement('div');
      row.className = 'tool-row collapsed';
      row.dataset.toolState = 'completed';
      row.dataset.toolName = toolName;
      row.onclick = toggleToolRow;

      row.innerHTML =
        '<div class="tool-row-head">' +
          '<span class="tool-row-icon">▸</span>' +
          '<span class="tool-state-icon">✅</span>' +
          '<strong class="tool-row-name">🛠️ ' + escapeHtml(toolName) + '</strong>' +
          '<span class="tool-row-args-preview">' + escapeHtml(argsPreview) + '</span>' +
          '<span class="tool-row-status" data-state="completed">完成' +
            (msg.index ? ' · #' + msg.index : '') + '</span>' +
        '</div>' +
        '<div class="tool-row-body">' +
          (msg.index ?
            '<div class="tool-row-meta">' +
              '<span class="msg-index-badge standalone">#' + msg.index + '</span>' +
              '<button class="backtrack-btn" title="回溯到此位置" data-index="' + msg.index + '" aria-label="回溯到位置 ' + msg.index + '">↩ 回溯</button>' +
            '</div>' : '') +
          (histArgs ?
            '<div class="tool-args-block">' +
              '<div class="tool-args-label">参数:</div>' +
              '<pre class="tool-args-json">' + escapeHtml(histArgs) + '</pre>' +
            '</div>' : '') +
          '<div class="tool-result-block">' +
            '<div class="tool-result-label">返回:</div>' +
            '<pre class="tool-result-body">' + escapeHtml(displayResult) + '</pre>' +
          '</div>' +
        '</div>';

      if (hostBody) {
        hostBody.appendChild(row);
      } else {
        // 无宿主：单独成组的裸行（不带头像/回溯，仅兜底展示）
        row.style.marginLeft = '12px';
        group.appendChild(row);
        container.appendChild(group);
      }
      return;

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

      var avatarHtml = '<div class="msg-side">' +
        '<div class="msg-avatar ' + roleConfig.cls + '" aria-hidden="true">' + roleConfig.avatar + '</div>' +
        (msg.index ? '<div class="msg-index-badge">#' + msg.index + '</div>' : '') +
        '</div>';

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
      // assistant 卡片登记为后续 tool 行的宿主；
      // 纯 tool_calls 无正文的卡片隐藏空内容区（与实时流式同构）
      if (msg.role === 'assistant') {
        _histToolHost = msgDiv.querySelector('.msg-body');
        if (msg.tool_calls && msg.tool_calls.length > 0 && !msg.content) {
          contentEl.classList.add('stream-pending');
        }
      }
      // 思考块：已落盘的 reasoning_content → 折叠态插在正文前（n4 历史回看）
      if (msg.reasoning_content) {
        var thinkEl = buildThinkBlock(msg.reasoning_content);
        thinkEl.classList.add('collapsed');
        thinkEl.querySelector('.think-msg-status').textContent =
          '思考 ' + msg.reasoning_content.length + ' 字';
        contentEl.parentNode.insertBefore(thinkEl, contentEl);
      }
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

  scrollToBottom(true);  // 历史加载/回溯 = 视图重置 → 强制滚底
}




// ── Mermaid 图渲染（任务图 /task view · md 内嵌 ```mermaid） ──
var _mermaidInit = false;

function initMermaid() {
  if (_mermaidInit || typeof mermaid === 'undefined') return;
  _mermaidInit = true;
  // strict: 标签内 HTML 被转义，防注入（任务描述来自 LLM/用户）
  mermaid.initialize({ startOnLoad: false, securityLevel: 'strict', theme: 'default' });
  var container = document.getElementById('messagesContainer') || document.body;
  initMermaidZoom(container);
  var pending = null;
  new MutationObserver(function () {
    if (pending) clearTimeout(pending);
    pending = setTimeout(runMermaid, 60);
  }).observe(container, { childList: true, subtree: true });
  runMermaid();
}

function runMermaid() {
  if (typeof mermaid === 'undefined') return;
  document.querySelectorAll('.mermaid:not([data-processed])').forEach(function (el) {
    mermaid.run({ nodes: [el] }).catch(function () {
      el.setAttribute('data-processed', 'error');
    });
  });
}

window.addEventListener('load', initMermaid);
if (document.readyState === 'complete') initMermaid();

// ── Mermaid 缩放/平移（滚轮/双指缩放 · 拖拽平移 · 双击复位） ──
// 事件委托挂稳定祖先（消息区重渲染不丢监听）；状态存 WeakMap；
// 内联：只变换 svg（卡片盒内裁剪）；预览浮层：整张卡片一起变换（底/边框/阴影随内容走）；
// 参照盒与视口保持 ≥_mzEdge 像素重叠防"拖丢"。
var _mzState = new WeakMap();
var _mzMin = 0.2, _mzMax = 6, _mzEdge = 48;
var _mzDrag = null;
var _mzPts = {};      // 活动触点 id → {x,y}（双指缩放用）
var _mzPinch = null;  // { c, d0, s0 } 双指缩放起点
var _mzClick = null; // pointerdown 起点，供 click 区分「点开预览」与拖拽
var _mzOpenAt = 0;   // 浮层打开时刻（吞掉双击的第二击）

function _mzSt(container) {
  var st = _mzState.get(container);
  if (!st) { st = { s: 1, x: 0, y: 0 }; _mzState.set(container, st); }
  return st;
}

// 事件目标 → 图卡片；浮层暗背景上也归到浮层里唯一那张卡
function _mzCardFrom(t) {
  if (!t || !t.closest) return null;
  var c = t.closest('.mermaid');
  if (c) return c.querySelector('svg') ? c : null;
  var ov = t.closest('.mz-overlay');
  if (ov) { c = ov.querySelector('.mermaid'); return c && c.querySelector('svg') ? c : null; }
  return null;
}

// 被变换元素 + 参照盒：浮层变换整卡（背景跟随），内联只变换 svg
function _mzParts(c) {
  if (c.closest('.mz-overlay')) return { target: c, r0el: c.closest('.mz-stage') };
  return { target: c.querySelector('svg'), r0el: c };
}

function _mzApply(container) {
  var m = _mzParts(container);
  if (!m.target || !m.r0el) return;
  var st = _mzSt(container);
  var r0 = m.r0el.getBoundingClientRect();
  for (var pass = 0; pass < 2; pass++) {
    m.target.style.transformOrigin = '0 0';
    m.target.style.transform = 'translate(' + st.x + 'px,' + st.y + 'px) scale(' + st.s + ')';
    var r = m.target.getBoundingClientRect();
    var ox = r.left - r0.left, oy = r.top - r0.top;
    var dx = 0, dy = 0;
    var minX = _mzEdge - r.width, maxX = r0.width - _mzEdge;
    if (minX <= maxX) dx = Math.min(maxX, Math.max(minX, ox)) - ox;
    var minY = _mzEdge - r.height, maxY = r0.height - _mzEdge;
    if (minY <= maxY) dy = Math.min(maxY, Math.max(minY, oy)) - oy;
    if (!dx && !dy) break;
    st.x += dx; st.y += dy;
  }
  container.classList.toggle('mz-zoomed', st.s !== 1 || st.x !== 0 || st.y !== 0);
}

// 以锚点（光标/两指中点）缩放的公共数学
function _mzZoomBy(c, ns, ax, ay) {
  var st = _mzSt(c);
  ns = Math.min(_mzMax, Math.max(_mzMin, ns));
  if (ns === st.s) return;
  var m = _mzParts(c);
  var r = m.target.getBoundingClientRect(); // 旧变换下的原点
  var k = ns / st.s;
  st.x += (ax - r.left) * (1 - k);  // 锚点下的内容点保持不动
  st.y += (ay - r.top) * (1 - k);
  st.s = ns;
  _mzApply(c);
}

function _mzWheel(e) {
  if (e.target && e.target.closest && e.target.closest('.mz-close')) return;
  var c = _mzCardFrom(e.target);
  if (!c) return;
  e.preventDefault();
  var dY = e.deltaY;
  if (e.deltaMode === 1) dY *= 16; else if (e.deltaMode === 2) dY *= 100;
  var st = _mzSt(c);
  _mzZoomBy(c, st.s * Math.exp(-dY * 0.0018), e.clientX, e.clientY);
}

function _mzDown(e) {
  if (e.button !== 0 || !e.target.closest) return;
  var c = _mzCardFrom(e.target);
  if (!c) return;
  _mzClick = { x: e.clientX, y: e.clientY };
  var inOv = !!c.closest('.mz-overlay');
  // 未缩放不劫持（保留文字选择）；预览浮层内例外（浮层专为缩放平移而生）
  if (!inOv && !c.classList.contains('mz-zoomed')) return;
  e.preventDefault();
  _mzPts[e.pointerId] = { x: e.clientX, y: e.clientY };
  if (Object.keys(_mzPts).length >= 2) {          // 第二指落下 → 双指缩放，压掉单指拖拽
    if (_mzDrag) { _mzDrag.c.classList.remove('mz-dragging'); _mzDrag = null; }
    if (!_mzPinch) _mzPinch = { c: c, d0: _mzPinchD0(), s0: _mzSt(c).s };
    return;
  }
  _mzDrag = { c: c, id: e.pointerId, x: e.clientX, y: e.clientY };
  c.classList.add('mz-dragging');
}

function _mzPinchPairs() {
  var ids = Object.keys(_mzPts);
  if (ids.length < 2) return null;
  return [_mzPts[ids[0]], _mzPts[ids[1]]];
}

function _mzPinchD0() {
  var p = _mzPinchPairs();
  if (!p) return 1;
  return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
}

function _mzMove(e) {
  if (_mzPts[e.pointerId]) _mzPts[e.pointerId] = { x: e.clientX, y: e.clientY };
  if (_mzPinch) {
    var p = _mzPinchPairs();
    if (!p) return;
    var d = Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1;
    _mzZoomBy(_mzPinch.c, _mzPinch.s0 * d / _mzPinch.d0, (p[0].x + p[1].x) / 2, (p[0].y + p[1].y) / 2);
    return;
  }
  if (!_mzDrag || e.pointerId !== _mzDrag.id) return;
  var st = _mzSt(_mzDrag.c);
  st.x += e.clientX - _mzDrag.x;
  st.y += e.clientY - _mzDrag.y;
  _mzDrag.x = e.clientX; _mzDrag.y = e.clientY;
  _mzApply(_mzDrag.c);
}

function _mzUp(e) {
  delete _mzPts[e.pointerId];
  if (_mzPinch) {
    var n = Object.keys(_mzPts).length;
    if (n >= 2) return;                        // 还有两指在，继续缩放
    var pc = _mzPinch.c;
    _mzPinch = null;
    if (n === 1) {                             // 剩一指 → 无缝转平移
      var id = Object.keys(_mzPts)[0];
      _mzDrag = { c: pc, id: id, x: _mzPts[id].x, y: _mzPts[id].y };
      pc.classList.add('mz-dragging');
    } else {
      pc.classList.remove('mz-dragging');
    }
    return;
  }
  if (!_mzDrag || e.pointerId !== _mzDrag.id) return;
  _mzDrag.c.classList.remove('mz-dragging');
  _mzDrag = null;
}

function _mzDbl(e) {
  var c = _mzCardFrom(e.target);   // 浮层暗背景双击同样复位那张卡
  if (!c) return;
  e.preventDefault();
  _mzState.set(c, { s: 1, x: 0, y: 0 });
  _mzApply(c);
}

function initMermaidZoom(container) {
  if (!container || container._mzBound) return;
  container._mzBound = true;
  container.addEventListener('wheel', _mzWheel, { passive: false });
  container.addEventListener('pointerdown', _mzDown);
  container.addEventListener('click', _mzClickOpen);
  container.addEventListener('dblclick', _mzDbl);
  window.addEventListener('pointermove', _mzMove);
  window.addEventListener('pointerup', _mzUp);
  window.addEventListener('pointercancel', _mzUp);
}

// ── 点开预览（单击未缩放的图 → 全屏浮层 · 滚轮/双指缩放 · 拖拽平移 · 双击复位 · Esc/✕/点空白关闭） ──
function _mzClickOpen(e) {
  var info = _mzClick;
  _mzClick = null;
  if (!info || Math.abs(e.clientX - info.x) > 5 || Math.abs(e.clientY - info.y) > 5) return; // 拖拽残留的 click 不开
  if (e.button !== undefined && e.button !== 0) return;
  var c = e.target && e.target.closest ? e.target.closest('.mermaid') : null;
  if (!c || !c.querySelector('svg')) return;
  if (c.closest('.mz-overlay')) return;               // 浮层内点击不开嵌套预览
  if (c.classList.contains('mz-zoomed')) return;      // 已缩放 = 平移/双击复位模式
  var sel = window.getSelection && window.getSelection();
  if (sel && String(sel)) return;                     // 正在选中文本不误开
  openMzPreview(c);
}

function openMzPreview(c) {
  closeMzPreview();
  var ov = document.createElement('div');
  ov.className = 'mz-overlay';
  var stage = document.createElement('div');
  stage.className = 'mz-stage';
  var clone = c.cloneNode(true);                      // 克隆已渲染 svg（data-processed 随迁，不触发重渲染）
  clone.classList.remove('mz-zoomed', 'mz-dragging');
  stage.appendChild(clone);
  var btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'mz-close';
  btn.setAttribute('aria-label', '关闭预览');
  btn.textContent = '✕';
  ov.appendChild(stage);
  ov.appendChild(btn);
  document.body.appendChild(ov);
  _mzOpenAt = Date.now();
  initMermaidZoom(stage);                             // 浮层复用同一套 wheel/pointer/dbl/click 委托
  btn.addEventListener('click', closeMzPreview);
  ov.addEventListener('click', function (ev) {        // 点空白（stage 背景/遮罩）关闭
    if (ev.target === ov || ev.target === stage) {
      if (Date.now() - _mzOpenAt < 300) return;       // 双击的第二击不误关
      closeMzPreview();
    }
  });
  document.addEventListener('keydown', _mzEsc);
}

function closeMzPreview() {
  var ov = document.querySelector('.mz-overlay');
  if (ov) ov.remove();
  document.removeEventListener('keydown', _mzEsc);
}

function _mzEsc(e) {
  if (e.key === 'Escape') closeMzPreview();
}
