/* ═══════════════════════════════════════════════════════════
   FP WebUI — 消息卡
   分组/用户卡/助手卡/系统卡/错误卡/排队气泡/复制/折叠 + 思考块构建
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { messagesEl, lastGroupEl, setLastGroupEl, liveMsgIndex, setLiveMsgIndex, setToolContainerEl } from './state.js';
import { getSb, setSb, updateStatusBar } from './statusbar.js';
import { scrollToBottom } from './scroll.js';
import { renderFileRefs, renderMarkdown, renderMath } from './markdown.js';
import { escapeHtml, formatTime } from './util.js';

export function getOrCreateMsgGroup(forcedRhythm) {
  if (!lastGroupEl || !document.body.contains(lastGroupEl) || forcedRhythm === 'loose') {
    setLastGroupEl(document.createElement('div'));
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

export function copyMsgContent(btnEl) {
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
export function toggleToolRow(event) {
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
export function addUserMessage(content, ephemeral) {
  var group = getOrCreateMsgGroup('loose');
  if (!ephemeral) { setLiveMsgIndex(liveMsgIndex + 1); }
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
      '<div class="msg-content">' + renderFileRefs(escapeHtml(content)) + '</div>' +
    '</div>';
  group.appendChild(msg);

  // 复制按钮事件
  msg.querySelector('.copy-btn').onclick = function(e) { e.stopPropagation(); copyMsgContent(this); };

  // 状态栏
  setSb({ msgs: getSb().msgs + 1 });
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
export function addQueuedMessage(content) {
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
  setLiveMsgIndex(liveMsgIndex + 1);
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
export function consumeQueuedBubble(content, ephemeral) {
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

export function clearQueuedFlags() {
  document.querySelectorAll('.queued-flag').forEach(function (el) { el.remove(); });
}

/**
 * 助手消息卡片。
 * @param {string} content 消息文本
 * @param {boolean} [ephemeral] true = 命令输出等不落盘消息：
 *   不计入回溯索引（liveMsgIndex）、不渲染回溯按钮/徽章、不登记为工具行宿主
 */
export function addAssistantMessage(content, ephemeral) {
  var group = getOrCreateMsgGroup();
  if (!ephemeral) { setLiveMsgIndex(liveMsgIndex + 1); }
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
    setToolContainerEl(msg.querySelector('.msg-body'));
  }

  var contentEl = msg.querySelector('.msg-content');
  if (content) {
    contentEl.innerHTML = renderMarkdown(content);
    renderMath();
  }

  scrollToBottom();
  return contentEl;
}

function toggleThinkMsg(event) {
  if (event.target.closest('.backtrack-btn') || event.target.closest('.msg-avatar')) return;
  event.currentTarget.classList.toggle('collapsed');
}

/** 构建思考块元素（流式与历史回看共用） */
export function buildThinkBlock(text) {
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

export function showError(msg) {
  var group = getOrCreateMsgGroup();
  var err = document.createElement('div');
  err.className = 'error-msg';
  err.setAttribute('role', 'alert');
  err.textContent = '❌ ' + msg;
  group.appendChild(err);
  scrollToBottom();
}

export function addSystemMessage(text) {
  var group = getOrCreateMsgGroup();
  var el = document.createElement('div');
  el.className = 'sys-msg';
  el.textContent = text;
  group.appendChild(el);
  scrollToBottom();
}


/* ── 排队对账表重置（clearUI 调用；表本体模块私有） ── */
export function resetQueue() { _queuedPending = []; }
