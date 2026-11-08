/* ═══════════════════════════════════════════════════════════
   FP WebUI — UI 渲染层（barrel）
   ───────────────────────────────────────────────────────────
   原 1568 行单体已按职责拆入 components/，本文件只做聚合导出：
   外部（app.js / main.js）的 import 面保持 53 个导出不变。
   组件之间互相 import 各自文件、不经过本 barrel（避免环）；
   本文件无顶层副作用，副作用都在各组件模块内。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

export * from './components/state.js';
export * from './components/util.js';
export * from './components/statusbar.js';
export * from './components/scroll.js';
export * from './components/markdown.js';
export * from './components/messages.js';
export * from './components/thinking.js';
export * from './components/tools.js';
export * from './components/stream.js';
export * from './components/control.js';
export * from './components/cmdpalette.js';
export * from './components/history.js';
export * from './components/mermaidzoom.js';
export * from './components/mermaid.js';
