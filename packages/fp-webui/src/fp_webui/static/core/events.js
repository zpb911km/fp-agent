/* ═══════════════════════════════════════════════════════════
   FP WebUI — WS 事件注册表（分发中枢）
   ─────────────────────────────────────────────────────────
   把 app.js 的 27-case switch 换成 on(type, fn) 注册：
   · 新事件 = 一处注册，不再改分发函数本体
   · 未知类型 → console.warn 兜底：不抛错（e2e test_06 要求
     页面可交互、无 JS error），也不静默吞（区别于旧行为）
   · 注册顺序无关，浏览器侧无状态依赖
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

const handlers = new Map();

/** 注册一个事件处理器。重复注册是编程错误 → warn（契约测试静态兜底双注册）。 */
export function on(type, fn) {
  if (handlers.has(type)) {
    console.warn('[events] 重复注册事件处理器:', type);
  }
  handlers.set(type, fn);
}

/**
 * 分发一条 WS 事件。
 * 未知类型：warn 一条（console warn 不进 e2e 错误账本），绝不抛错——
 * 单条坏事件不能打断整条消息流。
 */
export function dispatch(data) {
  const type = data && data.type;
  const fn = handlers.get(type);
  if (fn) {
    fn(data);
    return;
  }
  console.warn('[events] 未知事件类型已忽略:', type);
}

/** 已注册类型（调试/契约自省用）。 */
export function registeredTypes() {
  return Array.from(handlers.keys());
}
