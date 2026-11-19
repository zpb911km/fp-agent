/* ═══════════════════════════════════════════════════════════
   FP WebUI — 历史回看
   renderHistoryMessages：REST 历史 → 消息 DOM（两遍：工具映射→渲染）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { copyMsgContent, toggleToolRow, buildThinkBlock, renumberLiveMessages } from './messages.js';
import { renderMarkdown, renderMath } from './markdown.js';
import { scrollToBottom } from './scroll.js';
import { escapeHtml } from './util.js';

// ── 会话历史 ──

export function renderHistoryMessages(sid, messages) {
  var container = document.getElementById('messagesContainer');
  if (!container) return;
  // 历史块先建在 DocumentFragment 里，最后一次性插到容器最前（见函数末尾）。
  // 「先 append 到容器」的写法在异步 + 流式并发下会错序：REST 返回之前插进来的
  // 实时卡片/系统提示都在容器里，历史一 append 就落到了它们后面。
  var frag = document.createDocumentFragment();
  var sep = document.createElement('div');
  sep.className = 'history-separator';
  sep.textContent = '📜 历史记录';
  sep.dataset.fpHistory = '1';
  frag.appendChild(sep);

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
    group.dataset.fpHistory = '1';   // 幂等重建的凭据：重渲染前整批摘掉
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
      frag.appendChild(group);
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

      // 宿主：前一张助手卡片的 msg-body；缺失（历史截断）→ 兜底独立成组。
      // ⚠️ 判据必须是 frag.contains，**不能**是 document.body.contains：本函数把整批
      //    历史建在脱离文档的 DocumentFragment 里（末尾才 insertBefore 落位），此刻
      //    body.contains 恒为 false → 每一条工具行都被判成「无宿主」，裸挂到卡片外。
      //    真机症状：历史里工具行全部漏出助手卡片（#244）。反向验证见
      //    test_webui_history_tool_nesting_e2e.py::test_25。
      var hostBody = (_histToolHost && frag.contains(_histToolHost)) ? _histToolHost : null;

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
        frag.appendChild(group);
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
      } else {
        // user 消息：宿主作废。历史被截断（tool 消息的助手卡片缺失）时，孤儿工具行
        // 只能走裸行兜底——若沿用更早轮次的卡片，就会挂到**别人的**卡片里（错挂）。
        _histToolHost = null;
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

    frag.appendChild(group);
  });

  // ── 落位：历史永远锚定容器最前 ────────────────────────────
  // 语义上历史是「更早的内容」，而实时卡片/系统提示一律 appendChild（更晚）。
  // 只要落位是 insertBefore(firstChild)，顺序就与「REST 何时返回、期间谁插了
  // 什么」彻底解耦——agent 忙时切会话（历史还在路上，新 chunk 先建了卡）
  // 也不会再出现「新消息出现在『📜 历史记录』横幅上方」的倒置。
  // 幂等：先摘掉上一次渲染的历史节点，避免重连重拉叠加出第二套历史。
  var stale = container.querySelectorAll('[data-fp-history]');
  for (var s = 0; s < stale.length; s++) stale[s].remove();
  container.insertBefore(frag, container.firstChild);

  // ── 编号基准：历史末尾 index → 校准实时索引 ────────────────
  // 历史落地前建出来的实时卡片（agent 忙时 chunk 抢在 REST 之前）拿的是 clearUI
  // 归零后的临时号（#1、#2…），必须以历史末尾为基准重排，否则回溯索引整体错位。
  var base = 0;
  for (var m = messages.length - 1; m >= 0; m--) {   // 跳过 system 消息（index 为 null）
    if (messages[m].index != null) { base = messages[m].index; break; }
  }
  renumberLiveMessages(base);

  // 批量渲染公式
  if (allRendered.length > 0) {
    renderMath();
  }

  scrollToBottom(true);  // 历史加载/回溯 = 视图重置 → 强制滚底
}




