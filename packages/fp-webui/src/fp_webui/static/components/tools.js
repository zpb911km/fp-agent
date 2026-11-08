/* ═══════════════════════════════════════════════════════════
   FP WebUI — 工具行
   tool_call_id 驱动的参数/结果行（三层渲染链：注册表→元信息→JSON 兜底）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { liveMsgIndex, setLiveMsgIndex, _toolContainerEl } from './state.js';
import { _sbData, updateStatusBar } from './statusbar.js';
import { scrollToBottom } from './scroll.js';
import { addAssistantMessage, toggleToolRow } from './messages.js';
import { removeThinking } from './thinking.js';
import { escapeHtml } from './util.js';
import { getRenderer, fallbackArgsHtml, metaArgsHtml } from '../core/renderers.js';

// ── 工具调用追踪 —— tool_call_id → DOM 元素 ──
// 解决并行调用同名工具时按名称匹配错乱的问题
var toolCardMap = {};
// ── 工具行宿主容器 ──
// 方案C：紫卡已移除。tool_select 不再建卡，只确保当前轮有承载工具行的
// 助手卡片。有正文的轮次复用正文卡片；纯 tool_calls 轮懒建空卡片
//（空 .msg-content 挂 stream-pending 隐藏，只留 header + 工具行）。
export function ensureToolContainer() {
  if (_toolContainerEl && document.body.contains(_toolContainerEl)) return _toolContainerEl;
  var contentEl = addAssistantMessage('');
  contentEl.classList.add('stream-pending');
  return _toolContainerEl; // addAssistantMessage 内部已更新
}

// ── 工具行（tool_call_id 驱动匹配，解决并行调用同名工具错乱）──
// 方案C：降级为嵌在助手卡片内的紧凑行（一轮 = 一张卡）。
// 运行中展开（实时看参数），完成/失败自动折叠为单行，点击行可再展开。
export function addToolCall(name, args, tool_call_id) {
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
  setLiveMsgIndex(liveMsgIndex + 1);
  var idx = liveMsgIndex;

  var prettyArgs = '';
  var parsed = null;
  try {
    parsed = typeof args === 'string' ? JSON.parse(args) : args;
    prettyArgs = JSON.stringify(parsed, null, 2);
  } catch(e) {
    parsed = (typeof args === 'string') ? args : null;  // 解析失败 → 交付原始字符串（兑现 ctx 契约）
    prettyArgs = args || '{}';
  }
  // 折叠态单行的参数预览：紧凑 JSON 截断
  var argsPreview = prettyArgs.replace(/\s+/g, ' ').trim();
  if (argsPreview.length > 80) argsPreview = argsPreview.slice(0, 80) + '…';

  var container = ensureToolContainer();

  // ── 参数块：三层链 —— 手写渲染器 → 后端元信息 → JSON 兜底 ──
  //   ① 手写渲染器：某工具需要特殊可视化时才写（registerRenderer）
  //   ② 后端元信息：数据驱动，新增工具无需改前端源码
  //   ③ JSON 兜底：两者都没有时的最后防线，绝不丢数据
  var _renderedArgs = null;
  var _argsRenderer = getRenderer(name, 'args');
  if (_argsRenderer) {
    try {
      _renderedArgs = _argsRenderer({ name: name, args: parsed, rawArgs: String(args || ''), result: null, isError: false });
    } catch (e) { _renderedArgs = null; }  // 渲染器抛错 → 回落兜底，不拖垮工具行
  }
  if (_renderedArgs == null) {
    try {
      _renderedArgs = metaArgsHtml({ name: name, args: parsed, rawArgs: prettyArgs, escaped: escapeHtml });
    } catch (e) { _renderedArgs = null; }
  }
  var argsBlockHtml = _renderedArgs != null
    ? _renderedArgs
    : fallbackArgsHtml(prettyArgs, escapeHtml);

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
      '<div class="tool-args-block">' + argsBlockHtml + '</div>' +
    '</div>';

  container.appendChild(row);

  // ── 注册到映射表 ──
  toolCardMap[tool_call_id] = row;

  scrollToBottom();
}

// ── 更新工具行结果（tool_call_id 精确匹配）──
export function addToolResult(name, result, isError, tool_call_id) {
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

    // ── 结果块：注册表优先，未注册走通用兜底（绝不丢弃）──
    var _renderedResult = null;
    var _resRenderer = getRenderer(name, 'result');
    if (_resRenderer) {
      try {
        _renderedResult = _resRenderer({ name: name, args: null, rawArgs: '', result: displayResult, isError: !!isError });
      } catch (e) { _renderedResult = null; }  // 渲染器抛错 → 回落兜底
    }
    var resultHtml = _renderedResult != null
      ? _renderedResult
      : '<div class="tool-result-label">' + (isError ? '错误:' : '返回:') + '</div>' +
        '<pre class="tool-result-body">' + escapeHtml(displayResult) + '</pre>';

    var resBlock = document.createElement('div');
    resBlock.className = 'tool-result-block';
    resBlock.innerHTML = resultHtml;
    bodyEl.appendChild(resBlock);
  }

  // ── 完成即折叠为单行（方案C：运行中展开，结束后让出历史高度）──
  target.classList.add('collapsed');

  scrollToBottom();
}


/* ── 工具卡映射重置（clearUI 调用；映射表模块私有） ── */
export function resetToolCards() { toolCardMap = {}; }
