/* ═══════════════════════════════════════════════════════════
   FP WebUI — 启动装配（登录成功后的唯一入口）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { inputEl } from '../ui.js';
import { applyTheme, applyHalo } from './appearance.js';
import { connectWebSocket } from './ws.js';
import { fetchToolMeta, toolCardCount } from '../core/toolmeta.js';
import { LoadingState } from '../core/spinner.js';

// ── 初始化与登录流程 ──

export function initApp() {
  // 遮罩隐藏已在调用点完成（doLogin 路径本就先 hideLogin；
  // 自动登录路径在 app.js DOMContentLoaded 分支补调）——
  // 此处不再 import auth，避免 auth⇄bootstrap 循环依赖。
  // 恢复主题偏好
  var saved = localStorage.getItem('webui_theme');
  if (saved === 'light') applyTheme(true);
  // 恢复光环开关状态（同步按钮图标）
  applyHalo((typeof window.isHaloEnabled === 'function')
    ? window.isHaloEnabled()
    : (localStorage.getItem('webui_halo') === 'on'));
  connectWebSocket();
  // 场景③收口：首屏启动占位到 app 可交互（启动装配完成）即结束，
  // 不依赖 ws 连通（ws 挂掉时整页仍可用）；onopen 的 hidePage 为幂等兜底。
  // 返工①：hidePage 撤占位前先给主区接力行内占位——否则 hidePage→connected→
  // fetchSessionHistory 行内 show 之间的窗口里 messagesContainer 真空，
  // components.css 的 :empty::after 空态文本抢跑（用户报的"先见空态后见 spinner"）。
  // connected 分支拉历史则由 fetchSessionHistory 幂等接管；不拉则显式 hide 收场。
  var mainArea = document.getElementById('messagesContainer');
  if (mainArea) LoadingState.show(mainArea, '加载会话内容…');
  LoadingState.hidePage();
  // 工具卡元信息：必须在登录后拉（/api/ui/schema 需鉴权，启动时
  // 尚无 token 会 401）。成功后参数区由后端元信息驱动渲染。
  fetchToolMeta().then(function (ok) {
    if (ok) console.info('[ToolMeta] 工具卡元信息已加载:', toolCardCount(), '个');
  });
  inputEl.focus();
}