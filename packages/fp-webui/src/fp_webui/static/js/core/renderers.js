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
   元信息（type / required / desc）是给 LLM 看的 schema 定义，
   不是给人看的内容 —— 用户只关心"这次实际传了什么"。所以这里
   只借 card.params 的**顺序**，行内容一律是实参本身：
     · 按 schema 顺序列出**实际传入**的参数（参数名 = 实际值）；
     · 未传入的参数不占行（一个都没传才显示"未提供"占位）；
     · schema 之外的实参照列（插件动态参数，绝不丢数据）；
     · 类型 / 必填星号 / 参数说明一律不渲染。
   原始 JSON 折叠仍在下方：结构化视图不丢任何字节。

   返回 null 表示"无元信息可用，走 JSON 兜底"。 */
export function metaArgsHtml(ctx) {
  const card = getToolCard(ctx && ctx.name);
  if (!card || !Array.isArray(card.params) || card.params.length === 0) return null;

  const escaped = ctx.escaped || ((s) => String(s == null ? '' : s));
  const args = (ctx.args && typeof ctx.args === 'object' && !Array.isArray(ctx.args)) ? ctx.args : {};
  const raw = typeof ctx.rawArgs === 'string' && ctx.rawArgs ? ctx.rawArgs : '';

  const esc = (v) => {
    let s;
    try { s = (typeof v === 'string') ? v : JSON.stringify(v); } catch (e) { s = String(v); }
    s = String(s);
    if (s.length > 200) s = s.slice(0, 200) + '…';
    return '<code class="tool-args-value">' + escaped(s) + '</code>';
  };
  // 实参"已传入"判定：own 属性且值非 undefined（undefined = 未提供）
  const passed = (k) => Object.prototype.hasOwnProperty.call(args, k) && args[k] !== undefined;

  const seen = new Set();
  let rows = '';
  for (const p of card.params) {
    if (!p || !p.name) continue;
    seen.add(p.name);
    if (!passed(p.name)) continue;  // 未传入 → 不占行
    rows += '<div class="tool-args-meta-row">' +
      '<span class="tool-args-meta-name">' + escaped(p.name) + '</span>' +
      '<span class="tool-args-meta-eq"> = </span>' +
      '<span class="tool-args-meta-value">' + esc(args[p.name]) + '</span>' +
      '</div>';
  }
  // schema 之外的实参（如插件动态加的参数）—— 照列，绝不丢数据
  for (const k of Object.keys(args)) {
    if (seen.has(k) || !passed(k)) continue;
    rows += '<div class="tool-args-meta-row tool-args-meta-unknown">' +
      '<span class="tool-args-meta-name">' + escaped(k) + '</span>' +
      '<span class="tool-args-meta-eq"> = </span>' +
      '<span class="tool-args-meta-value">' + esc(args[k]) + '</span>' +
      '</div>';
  }
  // 一个实参都没有 → "未提供"占位（空参数区看着像渲染失败）
  if (!rows) {
    rows = '<div class="tool-args-meta-row"><em class="tool-args-absent">未提供</em></div>';
  }

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
