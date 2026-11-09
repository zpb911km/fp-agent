/* ═══════════════════════════════════════════════════════════
   FP WebUI — UI 渲染层（barrel）
   ───────────────────────────────────────────────────────────
   原 1568 行单体已按职责拆入 components/，本文件只做聚合导出：
   外部（app.js / main.js）的 import 面保持 53 个导出不变。
   组件之间互相 import 各自文件、不经过本 barrel（避免环）；
   组件内各自持有副作用，本文件尾部另聚合 halo / proj 两个
   副作用组件（见文件末注释）。
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

/* ── 副作用组件：halo / proj ─────────────────────────────────
   二者原是 index.html 的独立 <script>，模块化后曾由 main.js
   直接 import。它们本质是渲染层组件（光环 IIFE / 投影），归入
   ui 聚合面 —— main.js 顶层只面对 ui / app 两个入口。

   必须放在全部 export* **之后**：halo 靠猴补丁依赖 components
   先求值，此处顺序即原加载顺序 ui → halo → proj 的语义等价。 */
import './halo.js';
import './proj.js';
