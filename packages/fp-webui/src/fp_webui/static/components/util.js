/* ═══════════════════════════════════════════════════════════
   FP WebUI — 文本工具
   escapeHtml / formatTime（零依赖叶子）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

// ── 文本工具 ──

export function escapeHtml(text) {
  var div = document.createElement('div');
  div.textContent = text;
  return div.innerHTML;
}

export function formatTime(ts) {
  var d = new Date(ts * 1000);
  return d.toLocaleTimeString('zh-CN', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
}