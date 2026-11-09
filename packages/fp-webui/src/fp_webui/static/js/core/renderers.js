/* ═══════════════════════════════════════════════════════════
   FP WebUI — 工具卡渲染注册表

   对齐 TUI 侧已定的扩展开口（docs/dev/显示层.md）：
   未知 role / 未知结构走通用卡，而不是丢弃。

   动机：工具行原本是硬编码模板，每加一个要定制展示的工具都得
   改 ui.js 源码 —— 而这段 JS 没有测试保护，是自举的瓶颈。
   注册后：新增工具的定制渲染只需 registerRenderer，不必动 ui.js。

   边界（重要）：
     · 只接管「参数块 / 结果块」两处纯展示区。
     · head / 状态图标 / 折叠态 / 耗时由状态机管理（addToolResult
       会写 dataset.toolState、.tool-state-icon、.tool-row-status），
       自定义渲染器碰这些会破坏状态更新，故不开放。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { getToolCard } from './toolmeta.js';

/**
 * @typedef {(ctx: {
 *   name: string,          // 工具名
 *   args: any,             // 已解析的参数（解析失败则为原始字符串）
 *   rawArgs: string,       // 原始参数字符串
 *   result: string|null,   // 结果（result 渲染时提供，call 阶段为 null）
 *   isError: boolean       // 是否错误（result 渲染时提供）
 * }) => string|null} ToolRenderer
 *   返回 HTML 字符串；返回 null 表示"本渲染器不接管，走通用兜底"。
 */

/** @type {Map<string, {args?: ToolRenderer, result?: ToolRenderer}>} */
const registry = new Map();

/**
 * 注册某工具的定制渲染。
 * @param {string} name 工具名（与后端 tool 名一致）
 * @param {{args?: ToolRenderer, result?: ToolRenderer}} spec
 */
export function registerRenderer(name, spec) {
  if (!name || !spec) return;
  registry.set(name, spec);
}

/**
 * 取某工具的渲染器（未注册返回 null → 调用方走通用兜底）。
 * @param {string} name
 * @param {'args'|'result'} phase
 * @returns {ToolRenderer|null}
 */
export function getRenderer(name, phase) {
  const spec = registry.get(name);
  if (!spec) return null;
  return spec[phase] || null;
}

/** 已注册的工具名清单（供调试 / 后端 schema 对账） */
export function registeredTools() {
  return Array.from(registry.keys());
}

/* ── 后端元信息驱动的参数区（介于手写渲染器与 JSON 兜底之间）──
   关键：元信息只有参数的 name/type/desc，**没有值**；值在 args 里。
   所以必须两者合并 —— 只画元信息会丢掉实际传入的值，比 JSON
   更糟。这里按 schema 顺序列出每个参数的名称/类型/必填/说明，
   并附上实际值；args 里有但 schema 没有的参数也照列（不丢数据）。

   返回 null 表示"无元信息可用，走 JSON 兜底"。 */
export function metaArgsHtml(ctx) {
  const card = getToolCard(ctx && ctx.name);
  if (!card || !Array.isArray(card.params) || card.params.length === 0) return null;

  const escaped = ctx.escaped || ((s) => String(s == null ? '' : s));
  const args = (ctx.args && typeof ctx.args === 'object' && !Array.isArray(ctx.args)) ? ctx.args : {};
  const raw = typeof ctx.rawArgs === 'string' && ctx.rawArgs ? ctx.rawArgs : '';

  const esc = (v) => {
    if (v === undefined) return '<em class="tool-args-absent">未提供</em>';
    let s;
    try { s = (typeof v === 'string') ? v : JSON.stringify(v); } catch (e) { s = String(v); }
    s = String(s);
    if (s.length > 200) s = s.slice(0, 200) + '…';
    return '<code class="tool-args-value">' + escaped(s) + '</code>';
  };

  const seen = new Set();
  let rows = '';
  for (const p of card.params) {
    if (!p || !p.name) continue;
    seen.add(p.name);
    rows += '<div class="tool-args-meta-row">' +
      '<span class="tool-args-meta-name">' + escaped(p.name) +
        (p.required ? '<span class="tool-args-meta-req" title="必填">*</span>' : '') +
      '</span>' +
      '<span class="tool-args-meta-type">' + escaped(p.type || '') + '</span>' +
      '<span class="tool-args-meta-value">' + esc(args[p.name]) + '</span>' +
      (p.desc ? '<span class="tool-args-meta-desc">' + escaped(p.desc) + '</span>' : '') +
      '</div>';
  }
  // schema 之外的实参（如插件动态加的参数）—— 照列，绝不丢数据
  for (const k of Object.keys(args)) {
    if (seen.has(k)) continue;
    rows += '<div class="tool-args-meta-row tool-args-meta-unknown">' +
      '<span class="tool-args-meta-name">' + escaped(k) + '</span>' +
      '<span class="tool-args-meta-type">—</span>' +
      '<span class="tool-args-meta-value">' + esc(args[k]) + '</span>' +
      '</div>';
  }
  if (!rows) return null;

  let html = '<div class="tool-args-label">参数:</div><div class="tool-args-meta">' + rows + '</div>';
  // 原始 JSON 折叠在下方：结构化视图不丢任何字节
  if (raw) {
    html += '<details class="tool-args-raw"><summary>原始 JSON</summary>' +
      '<pre class="tool-args-json">' + escaped(raw) + '</pre></details>';
  }
  return html;
}

/* ── 通用兜底渲染（未知工具一律走这里，绝不丢弃）──────────────
   与 ui.js 原硬编码模板同构：参数 = 标签 + <pre> JSON。        */
export function fallbackArgsHtml(prettyArgs, escaped) {
  return '<div class="tool-args-label">参数:</div>' +
         '<pre class="tool-args-json">' + escaped(prettyArgs) + '</pre>';
}

export function fallbackResultHtml(result, isError, escaped) {
  return '<div class="tool-result-label">' + (isError ? '错误:' : '返回:') + '</div>' +
         '<pre class="tool-result-json">' + escaped(result) + '</pre>';
}
