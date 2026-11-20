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
import { conn as wsConn } from './features/ws.js';
import {
  doLogin, openSessions, closeSessions, newAgent, reloadAgent, clearSession,
  toggleHalo, toggleTheme, onFileInputChange, handleKeyDown, sendMessage,
  onSessionsSearchInput, toggleSidebar, refreshSessions
} from './app.js';

/* ── 内联事件属性的全局挂载 ──────────────────────────────────
   index.html 有 16 处内联事件属性（onclick="doLogin()" 等）。
   HTML 属性在**全局作用域**求值，而 ES Module 的顶层函数默认
   不挂 window —— 若不集中暴露，这 16 处会全部静默失效。

   这里选择集中挂载而非改写为 addEventListener：改动面最小、
   可被 e2e 立即验证；这 17 个符号是 HTML 契约的必然结果，
   不算泄漏。其余 96 个原全局符号已随模块化收敛为模块局部。

   （后续若要彻底消除内联属性以顺带消除 XSS 面，应整体改为
     addEventListener 绑定，届时本块可整段删除。） */
Object.assign(window, {
  doLogin, openSessions, closeSessions, newAgent, reloadAgent, clearSession,
  toggleHalo, toggleTheme, onFileInputChange, handleKeyDown, sendMessage,
  onSessionsSearchInput, autoResize, handleInputFocus, toggleCmdPalette,
  toggleSidebar, refreshSessions
});

/* ── 测试接缝（e2e 专用）─────────────────────────────────────
   与 window.__fp_dispatch 同一模式：模块收敛后内部状态不再
   暴露于全局词法环境，测试需通过显式探针访问。

   registerRenderer 暴露于此是为了验证自举能力：e2e 在运行时
   注册定制渲染器，证明"新增工具卡无需改 ui.js 源码"。 */
import { initBuildHint } from './core/build.js';
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
/* 前端构建戳：此刻（登录前）就是本页的基准——页面若停在旧模块上，
   后续任何一次 WS 连上/重连都会对比出"磁盘更新了"，在状态栏露出刷新入口。 */
initBuildHint();

/* ── 诊断环 + 指认式报障（core/diag + core/diagui）─────────────
   ① 上传函数注入：core 不许反向依赖 features（依赖分层），故由入口把
      uploadFile 递进去。复用 filehub 的待发标记——指认出的 json 会自动
      挂在输入框上方，用户按 ➤ 即发，不新增任何流程。
   ② 关闭开关：URL 加 ?diag=0（移动端无控制台时的唯一可行入口），
      或 localStorage.fp_diag='0'（桌面）。关闭后 diagui 也一并不装。
   ③ 顺序：diag 先于 diagui —— 前者装环与冻结门，后者的长按入口依赖它。 */
import { init as diagInit } from './core/diag.js';
import { init as diaguiInit } from './core/diagui.js';
import { uploadFile } from './features/upload.js';
if (diagInit({ upload: uploadFile })) diaguiInit();

window.__fp_test = {
  closeSocket: closeSocket,
  // #228：连接代际/建连计数快照——e2e 用它断言「重连期不并行开第二条」
  wsStats: function () {
    return { created: wsConn.created, gen: wsConn.gen, lastPingTime: wsConn.lastPingTime,
             immediateReconnect: wsConn.immediateReconnect };
  },
  registerRenderer: registerRenderer,
  registeredTools: registeredTools,
  fetchToolMeta: fetchToolMeta,
  toolCardCount: toolCardCount,
  toolCardNames: toolCardNames,
  getToolCard: getToolCard,
  toolMetaLoaded: isLoaded
};
