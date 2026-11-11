/* ═══════════════════════════════════════════════════════════
   FP WebUI — 装配层（跨层接线 + 启动 + 对外 re-export）
   ───────────────────────────────────────────────────────────
   原 app.js(1179 行/9 职责) 按职责拆入 features/，本文件只做：
   · core/handlers 跨层注册（send/cancel）
   · DOMContentLoaded 启动分支
   · 对 main.js 的 re-export（import 面不变）
   features 依赖方向：auth→bootstrap→ws→sessions→history（无环）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { handlers } from './core/handlers.js';
import { setOnUnauthorized, isAuthed } from './core/http.js';
import { closeCmdPalette, scrollToBottom } from './ui.js';
import { store } from './core/store.js';
import { sendMessage } from './features/compose.js';
import { cancelMessage, probeWake } from './features/ws.js';
import { showLogin, hideLogin } from './features/auth.js';
import { initApp } from './features/bootstrap.js';
import { closeSessions } from './features/sessions.js';
import './features/wire.js';   // 副作用：27 个 on() 注册 + __fp_dispatch 接缝

/* ── 对 main.js 的 re-export（16 内联符号契约保持不变）── */
export { doLogin } from './features/auth.js';
export { openSessions, closeSessions, onSessionsSearchInput, toggleSidebar, refreshSessions } from './features/sessions.js';
export { newAgent, reloadAgent, clearSession } from './features/agent.js';
export { toggleTheme, toggleHalo } from './features/appearance.js';
export { onFileInputChange } from './features/upload.js';
export { sendMessage, handleKeyDown } from './features/compose.js';

/* ── 注册跨层 handler（消除 ui → app 反向依赖，见 core/handlers.js）── */
handlers.send = sendMessage;
handlers.cancel = cancelMessage;
setOnUnauthorized(showLogin);

document.addEventListener('DOMContentLoaded', function() {
  if (isAuthed()) {
    hideLogin();   // 带 token 刷新：启动即隐藏遮罩（原在 initApp 内，拆分时上移）
    initApp();
  } else {
    showLogin();
  }

  // 窄屏抽屉遮罩点击关闭（原 #sessionsModal 浮层遮罩的替代）
  document.getElementById('sidebarScrim').addEventListener('click', closeSessions);

  // ESC 关闭（命令面板 → 会话模态框）
  document.addEventListener('keydown', function(e) {
    if (e.key === 'Escape') {
      if (!document.getElementById('cmdPalette').hidden) {
        closeCmdPalette();
      } else {
        closeSessions();
      }
    }
  });

  // 点击输入区外部关闭命令面板
  document.addEventListener('click', function(e) {
    if (document.getElementById('cmdPalette').hidden) return;
    if (!e.target.closest('.input-area')) {
      closeCmdPalette();
    }
  });

  // 窗口 resize 时保持滚动在底部（键盘弹出场景）
  var resizeTimer = null;
  window.addEventListener('resize', function() {
    if (resizeTimer) clearTimeout(resizeTimer);
    resizeTimer = setTimeout(function() {
      if (!store.getState().processing) scrollToBottom(true);
    }, 150);
  });

  // ── 休眠唤醒快速续传 ──
  // 页面重新可见时立即探测/重连，不等 3s 定时器与 10s ping 周期：
  //   · 连接已死 → 立即重连（后端任务一直没停，connected 快照续传状态）
  //   · 半开连接（对端已忘但本端仍 OPEN）→ ping 探测，5s 无 pong 强制关闭
  // #228：探活/重连的全部判断收敛到 ws.js 的 probeWake / reconnectNow——
  // 探针绑定「发起时那条连接 + 代际」，不再用活绑定 ws 误杀重连后的新连接。
  document.addEventListener('visibilitychange', function() {
    if (document.visibilityState !== 'visible') return;
    probeWake();
  });

  // 阻止 iOS 橡皮筋滚动导致页面整体上移
  document.body.addEventListener('touchmove', function(e) {
    if (e.target.closest('.messages-container') || e.target.closest('.sidebar-body')) return;
    // 允许这些容器内部滚动
  }, { passive: true });
});