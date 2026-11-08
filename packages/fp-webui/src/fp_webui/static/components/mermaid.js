/* ═══════════════════════════════════════════════════════════
   FP WebUI — Mermaid 渲染
   任务图/内嵌图渲染（顶层挂 load 监听 + MutationObserver）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { initMermaidZoom } from './mermaidzoom.js';

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

