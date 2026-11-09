/* ═══════════════════════════════════════════════════════════
   FP WebUI — 命令面板
   Ctrl+K 命令选择器（顶层挂 #cmdList 三个委托监听）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { inputEl } from './state.js';
import { autoResize } from './scroll.js';
import { escapeHtml } from './util.js';
import { authFetch } from '../core/http.js';

// ── 命令面板 ──

var commandsCache = [];
var cmdHighlightIdx = -1;

export function toggleCmdPalette() {
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

export function closeCmdPalette() {
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

export function navigateCmdList(direction) {
  if (!commandsCache || commandsCache.length === 0) return;
  var total = commandsCache.length;
  cmdHighlightIdx += direction;
  if (cmdHighlightIdx < 0) cmdHighlightIdx = total - 1;
  if (cmdHighlightIdx >= total) cmdHighlightIdx = 0;
  renderCmdList();
}

export function confirmCmdSelection() {
  if (cmdHighlightIdx < 0) return;
  selectCmd(cmdHighlightIdx);
}

