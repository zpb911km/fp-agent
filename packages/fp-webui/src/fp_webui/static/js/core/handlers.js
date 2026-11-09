/* ═══════════════════════════════════════════════════════════
   FP WebUI — 跨层 handler 注册表
   消除 ui → app 的反向依赖：ui.js 需要触发"发送/取消"，
   但这两个函数定义在 app.js（重度依赖 ui.js）。
   若直接 import 会形成 ui ⇄ app 循环，且 ui.js 顶层
   `sendBtn.onclick = sendMessage` 会在 app.js 尚未求值时
   撞上 TDZ。
   改为：ui 只调 handlers.send()，app 启动时注册实现。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

export const handlers = {
  send: null,     // 由 app.js 注册为 sendMessage
  cancel: null    // 由 app.js 注册为 cancelMessage
};
