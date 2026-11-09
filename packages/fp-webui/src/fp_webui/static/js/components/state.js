/* ═══════════════════════════════════════════════════════════
   FP WebUI — 共享状态
   跨组件可变状态 + 跨模块写入 setter + DOM 引用（叶子模块，只依赖 core/hooks）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { hooks } from '../core/hooks.js';

// ── 状态 ──
export let ws = null;
export let sessionId = null;
export let reconnectTimer = null;
export let lastGroupEl = null;

// ── 当前轮工具行宿主：助手卡片的 .msg-body ──
// 工具行内嵌于助手卡片（方案C），不再各自成卡。
// 纯 tool_calls 轮（无正文）懒建空卡片承载。
export var _toolContainerEl = null;

/* ── 状态变更通知（替代 halo.js 原猴补丁 wrap()）──
   halo 需要在 setProcessing/showThinking/removeThinking 后重新
   同步光环状态。ESM 下猴补丁失效（模块函数不挂 window），改为主动通知。 */
export function notifyUiStateChanged() {
  if (hooks.uiStateChanged) hooks.uiStateChanged();
}

/* ── 跨模块写入 setter ──────────────────────────────────────
   ESM 的 import 绑定是只读的（活绑定可读、不可赋值）。
   这 7 个状态 app.js 也写，故提供 setter；读取仍直接 import。 */
export function setWs(v) { ws = v; }
/* 测试接缝：真实关闭 WebSocket（e2e 用它模拟断线以验证重连）。
   ws 已收敛为模块局部，测试不能再从全局词法环境读它，
   故提供显式探针（与 app.js 的 window.__fp_dispatch 同一模式）。 */
export function closeSocket() { if (ws) ws.close(); }
export function setSessionId(v) { sessionId = v; }
export function setReconnectTimer(v) { reconnectTimer = v; }
export function setLastGroupEl(v) { lastGroupEl = v; }
export function setPendingBacktrack(v) { _pendingBacktrack = v; }
export function setPendingHistoryRefresh(v) { _pendingHistoryRefresh = v; }
export function setLiveMsgIndex(v) { liveMsgIndex = v; }
// ── DOM 引用 ──
export const messagesEl = document.getElementById('messagesContainer');
export const inputEl = document.getElementById('inputField');
export const sendBtn = document.getElementById('sendBtn');
export const sessionLabel = document.getElementById('sessionLabel');
export const modelBadge = document.getElementById('modelBadge');
export const statusEl = document.getElementById('connectionStatus');
export var _pendingBacktrack = false;
// turn 进行中收到 history_changed → 只记账，等 done/cancelled/error 收尾再重拉。
// 立刻 clearUI 会抹掉正在流式生成的卡片，且此刻落盘历史不含本轮，重拉必失配。
export var _pendingHistoryRefresh = false;
export var liveMsgIndex = 0;

/* ── 其余跨模块写入 setter（拆分新增） ── */
export function setToolContainerEl(v) { _toolContainerEl = v; }
