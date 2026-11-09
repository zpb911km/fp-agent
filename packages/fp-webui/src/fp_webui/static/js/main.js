/* ═══════════════════════════════════════════════════════════
   FP WebUI — ESM 入口
   ───────────────────────────────────────────────────────────
   原 index.html 用 4 个同步 <script> 按序加载：
       ui.js → halo.js → proj.js → app.js
   顺序耦合是隐式的（只有一行注释说明"顺序重要"），且 halo
   靠猴补丁依赖 ui 先执行。改为 module 后由 import 图显式表达，
   module 天然 defer，DOM 已解析的前提仍然成立。

   halo.js / proj.js 是渲染层组件，已归入 ui.js 聚合（其尾部
   副作用 import），本入口顶层只面对 ui / app 两个入口。

   import 顺序 = 原加载顺序，保持语义等价。
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import './ui.js';
import './app.js';

import { autoResize, handleInputFocus, toggleCmdPalette, closeSocket } from './ui.js';
import {
  doLogin, openSessions, closeSessions, newAgent, reloadAgent, clearSession,
  toggleHalo, toggleTheme, onFileInputChange, handleKeyDown, sendMessage,
  onSessionsSearchInput
} from './app.js';

/* ── 内联事件属性的全局挂载 ──────────────────────────────────
   index.html 有 15 处内联事件属性（onclick="doLogin()" 等）。
   HTML 属性在**全局作用域**求值，而 ES Module 的顶层函数默认
   不挂 window —— 若不集中暴露，这 15 处会全部静默失效。

   这里选择集中挂载而非改写为 addEventListener：改动面最小、
   可被 e2e 立即验证；这 15 个符号是 HTML 契约的必然结果，
   不算泄漏。其余 96 个原全局符号已随模块化收敛为模块局部。

   （后续若要彻底消除内联属性以顺带消除 XSS 面，应整体改为
     addEventListener 绑定，届时本块可整段删除。） */
Object.assign(window, {
  doLogin, openSessions, closeSessions, newAgent, reloadAgent, clearSession,
  toggleHalo, toggleTheme, onFileInputChange, handleKeyDown, sendMessage,
  onSessionsSearchInput, autoResize, handleInputFocus, toggleCmdPalette
});

/* ── 测试接缝（e2e 专用）─────────────────────────────────────
   与 window.__fp_dispatch 同一模式：模块收敛后内部状态不再
   暴露于全局词法环境，测试需通过显式探针访问。

   registerRenderer 暴露于此是为了验证自举能力：e2e 在运行时
   注册定制渲染器，证明"新增工具卡无需改 ui.js 源码"。 */
import { registerRenderer, registeredTools } from './core/renderers.js';
import { store } from './core/store.js';

/* 状态树接缝：halo.js（IIFE，经 import 图先于本文件求值）需读
   store.sb 驱动光环双色；e2e 也经此断言状态树。与 __fp_dispatch 同模式。 */
window.__fp_store = store;
import { fetchToolMeta, toolCardCount, toolCardNames, isLoaded, getToolCard } from './core/toolmeta.js';

/* ── 工具卡元信息的拉取时机 ───────────────────────────────────
   注意：这里**不**调用 fetchToolMeta()。/api/ui/schema 需鉴权，
   启动时尚无 token 会 401（实测 e2e 抓到过）。拉取统一放在
   app.js 的 initApp() —— 登录成功后的唯一入口。

   本模块只负责把接口暴露到测试接缝，供 e2e 断言后端下发量。 */
window.__fp_test = {
  closeSocket: closeSocket,
  registerRenderer: registerRenderer,
  registeredTools: registeredTools,
  fetchToolMeta: fetchToolMeta,
  toolCardCount: toolCardCount,
  toolCardNames: toolCardNames,
  getToolCard: getToolCard,
  toolMetaLoaded: isLoaded
};
