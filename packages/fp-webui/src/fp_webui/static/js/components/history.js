/* ═══════════════════════════════════════════════════════════
   FP WebUI — 历史回看
   renderHistoryMessages：REST 历史 → 消息 DOM（两遍：工具映射→渲染）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { copyMsgContent, toggleToolRow, buildThinkBlock } from './messages.js';
import { renderMarkdown, renderMath } from './markdown.js';
import { scrollToBottom } from './scroll.js';
import { escapeHtml } from './util.js';

// ── 会话历史 ──

export function renderHistoryMessages(sid, messages) {
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
        roleConfig = { avatar: '<img src=\"/static/assets/favicon.png\" width=\"18\" height=\"19\" alt=\"\" style=\"vertical-align:middle\">', author: 'FP', cls: 'assistant' };
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




