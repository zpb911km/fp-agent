/* ═══════════════════════════════════════════════════════════
   FP WebUI — 状态树（Store）

   动机：UI 状态原本住在 DOM 里——"谁展开了""第几轮""有没有
   流在进行"全靠读元素属性和全局变量反推。每加一个功能就要和
   已有元素的历史状态打一次架，回归面随功能数超线性增长。

   这里把可变状态收进一棵状态树，视图只做纯渲染：
   状态变 → 通知订阅者 → 渲染函数按新状态重画。
   渲染必须幂等：同一状态渲染 N 次结果一致（不重复建卡）。

   零依赖、零构建：纯 ES module，分发物即源码。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

/**
 * 创建一个极简 store：状态 + 订阅 + 变更通知。
 * @param {object} initial 初始状态树
 */
export function createStore(initial) {
  let state = Object.assign({}, initial);
  const listeners = new Set();

  return {
    /** 取当前状态（只读视图，勿直接改） */
    getState() { return state; },

    /**
     * 合并式更新：patch 与当前状态浅合并。
     * 状态未实际变化时不通知（避免无谓重渲染）。
     * @param {object|function} patch 状态补丁，或 (prev) => patch
     * @returns {object} 更新后的状态
     */
    setState(patch) {
      const next = typeof patch === 'function' ? patch(state) : patch;
      let changed = false;
      for (const k in next) {
        if (state[k] !== next[k]) { changed = true; break; }
      }
      if (!changed) return state;
      state = Object.assign({}, state, next);
      for (const fn of listeners) {
        try { fn(state); } catch (e) { console.error('[store] 订阅者异常', e); }
      }
      return state;
    },

    /**
     * 订阅状态变化。立即以当前状态回调一次（便于初始化渲染）。
     * @param {(state: object) => void} fn
     * @returns {() => void} 退订函数
     */
    subscribe(fn) {
      listeners.add(fn);
      try { fn(state); } catch (e) { console.error('[store] 订阅者异常', e); }
      return () => listeners.delete(fn);
    }
  };
}

/* ── 全局状态树 ────────────────────────────────────────────
   pendingAsk: 待回复的 ask 卡片元数据 {ask_id, prompt, options, suggest}
               null = 无待回复。归一化进状态后，渲染函数据它幂等建/撤卡，
               修掉"重连收到 pending_ask 就叠一张新卡"的实测 bug。
   processing: 本轮是否在运行（原 components/control.js 的 export let，
               读点 8 处散落 app.js → 收进状态树，读走 getState()）。
   sb:         状态栏数据（原 statusbar.js 的 _sbData 对象就地改，
               写点 10 处散落 5 个组件 → setSb 归一写入）。            */
export const store = createStore({
  pendingAsk: null,
  processing: false,
  sb: {
    latency: null,
    toolsActive: 0,
    toolsTotal: 0,
    msgs: 0,
    state: 'idle'   // idle | thinking | processing
  }
});
