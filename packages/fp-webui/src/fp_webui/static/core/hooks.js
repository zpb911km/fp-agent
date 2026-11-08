/* ═══════════════════════════════════════════════════════════
   FP WebUI — 跨层 hook 注册表
   用于替代 halo.js 原来的猴补丁 wrap()：

     window[name] = function(){ orig.apply(...); syncFromSbData(); }

   该写法在 ESM 下必然静默失效——模块函数不再挂 window，
   且同步 <script> 必先于 defer 的 module 执行，halo 求值时
   ui.js 的三个函数还不存在（typeof orig !== 'function' 直接 return）。
   改为：ui.js 在状态函数末尾主动通知，halo 注册接收。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

export const hooks = {
  /* setProcessing / showThinking / removeThinking 被调用后触发；
     halo 注册为 syncFromSbData（延迟一帧，见 halo.js）。 */
  uiStateChanged: null
};
