# FP WebUI 前端 ESM 改造 · 清点与拆分方案

> 只读审计。范围：`packages/fp-webui/src/fp_webui/static/`（index.html 176 行 / app.js 1094 / ui.js 1513 / halo.js 589 / proj.js 403 / style.css 2282）

## 1. index.html 加载顺序

| 位置 | 脚本 | defer/async | 说明 |
|---|---|---|---|
| `index.html:17-25` | marked / DOMPurify / mermaid / katex / auto-render (CDN) | **全部 defer** | 5 个第三方库，defer 保证在 DOMContentLoaded 前按序执行 |
| `index.html:171` | `/static/ui.js` | 无（body 末尾同步） | **顺序第 1** |
| `index.html:172` | `/static/halo.js` | 无 | 第 2 |
| `index.html:173` | `/static/proj.js` | 无 | 第 3 |
| `index.html:174` | `/static/app.js` | 无 | 第 4（最后） |

- 顺序依赖的**唯一显式声明**：`index.html:168-170` 注释 `脚本加载（顺序重要：ui.js → halo.js → proj.js → app.js）`
- 4 个本地脚本**无 defer/async**，靠"位于 `</body>` 前"保证 DOM 已解析。
- **无内联 `<script>` 块**（`grep '<script>'` 零命中）——但有 **15 个内联事件属性**（见 §6）。

## 2. 每个 JS 文件的职责边界

| 文件 | 行数 | 包裹 | 职责（文件头注释） | 顶层符号 |
|---|---|---|---|---|
| `app.js` | 1094 | **裸全局** | 认证 / WebSocket / 会话管理 / 初始化 / 键盘事件（`app.js:1-6`） | 37 fn + 10 var |
| `ui.js` | 1513 | **裸全局** | 共享状态 / DOM 渲染 / 命令面板 / 消息构建（`ui.js:1-6`） | 76 fn + 23 var + 7 let + 7 const |
| `halo.js` | 589 | **IIFE**（`halo.js:22`）+ `'use strict'` | 迭代器光环 #2139 状态机移植 | 内部 23 fn（不外泄），仅 3 个 window 暴露 |
| `proj.js` | 403 | **IIFE**（`proj.js:20`）+ `'use strict'` | 投影层（屏幕前方投影仪影像） | 内部 18 fn，仅 `window.__proj` 暴露 |

**app.js 分段**：ask 卡片 `9-70` / WS 延迟与断连续传 `71-96` / 登录 `97-137` / 工具函数+WS 事件分发 `138-504`（`handleEvent` 巨型 `225-504`）/ 文件上传 `505-560` / 发送 `561-634` / 命令面板入口 `635-681` / 会话管理 `682-934` / **switchSession 重写 `935-957`** / 主题 `958-977` / 光环开关 `978-1006` / 初始化 `1007-1093`

**ui.js 分段**：状态 `12-71` / DOM 引用 `72-79` / 认证 `80-97` / 滚底 `98-129` / 消息渲染 `130-460` / 工具行 `461-637` / **流式渲染 `638-926`** / 命令面板 `927-1053` / 会话历史 `1054-1260` / Mermaid `1261-1290` / **Mermaid 缩放预览 `1291-1513`**

## 3. 跨文件依赖矩阵

### 3.1 app.js → ui.js（**48 个符号，重度单向**）
- **函数 33**：`updateStatusBar, getToken, setToken, clearToken, isAuthed, escapeHtml, scrollToBottom, autoResize, getOrCreateMsgGroup, addUserMessage, addQueuedMessage, consumeQueuedBubble, addAssistantMessage, ensureToolContainer, addToolCall, addToolResult, showThinking, removeThinking, endStreamTurn, startStreamRound, streamThinkToken, streamContentChunk, finalizeStreamRound, finalizeStreamRoundForDone, showError, setProcessing, clearUI, toggleCmdPalette, closeCmdPalette, navigateCmdList, confirmCmdSelection, addSystemMessage, renderHistoryMessages`
- **状态 15**：`ws, sessionId, processing, reconnectTimer, lastGroupEl, toolCardMap, _toolContainerEl, _sbData, messagesEl, inputEl, sendBtn, sessionLabel, modelBadge, statusEl, commandsCache, cmdHighlightIdx, _pendingBacktrack, _pendingHistoryRefresh, liveMsgIndex, _queuedPending, _stream, _turnSeq`

### 3.2 ui.js → app.js（**仅 3 个真实调用，反向极轻**）
- `authFetch` ← `ui.js:940`（拉 `/api/commands`）
- `cancelMessage` ← `ui.js:894`（`sendBtn.onclick = cancelMessage`）
- `sendMessage` ← `ui.js:904`（`sendBtn.onclick = sendMessage`）
- ⚠️ `fetchSessionHistory` 出现在 `ui.js:915` **但只是行尾注释**，非真实依赖。

### 3.3 halo.js 的耦合（**猴补丁 + window 桥**）
- `halo.js:554-556` `wrap('setProcessing') / wrap('showThinking') / wrap('removeThinking', …)` —— **运行时改写 ui.js 挂到 window 的函数**，必须在 ui.js 之后执行。
- `halo.js:540` `typeof _sbData !== 'undefined'` 防御性读 ui.js 状态（`halo.js:539-542` `syncFromSbData`）。
- `halo.js:534-536` 写出 `window.setHaloEnabled / isHaloEnabled / setHaloState`。

### 3.4 app.js ↔ halo.js
- `app.js:987-988` `window.setHaloEnabled(on)`；`app.js:1001-1002`、`app.js:1018-1019` `window.isHaloEnabled()`（**均带 typeof 防御**）。
- `app.js:443` 读写 `window._cancelTimeout`（自己写的，非跨文件）。

### 3.5 proj.js 的耦合
- `proj.js:280` 读 `window.__haloLight`（halo.js:427-428 写入）
- `proj.js:281` 读 `window.isHaloEnabled && !window.isHaloEnabled()`
- `proj.js:390-402` 写 `window.__proj = { set, stats }`（控制台调参入口）
- **ui.js / app.js 均不引用 proj.js**（零反向依赖）

### 3.6 app.js 内部猴补丁
- `app.js:936-937` `var _origSwitchSession = switchSession; switchSession = function(sid){…}` —— 同文件内重绑定全局函数（非跨文件，但 ESM 下 `function` 声明是模块局部，重绑定语义会变）。

**依赖图**：`ui ← app`（48）· `ui → app`（3）· `halo ─猴补丁→ ui`（3）· `app ⇄ halo`（window 桥）· `proj → halo`（2）· `proj ↮ ui/app`（零）

## 4. 全局符号清单

**合计 113 fn（37+76）+ 33 var（10+23）+ 7 let + 7 const = 160 顶层符号**（halo/proj 因 IIFE 不计入）。

### app.js — 37 fn
- **被跨文件共用（4）**：`authFetch, cancelMessage, sendMessage, fetchSessionHistory`（后者仅被 ui.js 注释提及）
- **仅本文件（33）**：`showAskCard, clearAskCard, replyAsk, withAuth, showLogin, hideLogin, doLogin, connectWebSocket, handleEvent, onFileInputChange, uploadFile, removePendingFile, renderPendingFiles, handleKeyDown, openSessions, closeSessions, renderSessionItem, onSessionsSearchInput, runSessionsSearch, loadSessionsList, switchSession, deleteSession, reloadAgent, clearSession, newAgent, flushPendingHistoryRefresh, backtrackTo, applyTheme, toggleTheme, syncHaloBtn, applyHalo, toggleHalo, initApp`
- **10 var**：`_finalContentAlreadyShown@9, _pendingAsk@13, _lastPingTime@72, _pingInterval@73, _lastSeq@76, _runId@77, _wakeProbe@78, _pendingFiles@508, _sessionsSearchTimer@730, _origSwitchSession@936`

### ui.js — 76 fn
- **被跨文件共用（33）**：`updateStatusBar, getToken, setToken, clearToken, isAuthed, escapeHtml, scrollToBottom, autoResize, getOrCreateMsgGroup, addUserMessage, addQueuedMessage, consumeQueuedBubble, addAssistantMessage, ensureToolContainer, addToolCall, addToolResult, showThinking, removeThinking, endStreamTurn, startStreamRound, streamThinkToken, streamContentChunk, finalizeStreamRound, finalizeStreamRoundForDone, showError, setProcessing, clearUI, toggleCmdPalette, closeCmdPalette, navigateCmdList, confirmCmdSelection, addSystemMessage, renderHistoryMessages`
  - 其中 **3 个被 halo.js 猴补丁**：`setProcessing, showThinking, removeThinking`
  - 其中 **3 个被 index.html 内联属性调用**：`autoResize, handleInputFocus, toggleCmdPalette`
- **仅本文件（43）**：`formatTime, _isNearBottom, handleInputFocus, renderFileRefs, handleFileRefClick, markMissing, renderMarkdown, _renderMarkdownBase, renderMath, legacyCopyText, copyMsgContent, toggleToolRow, promoteQueuedBubble, clearQueuedFlags, isCurrentTurnRound, thinkElapsedMs, renderThinkStatus, startThinkTicker, stopThinkTicker, foldThinkEl, toggleThinkMsg, buildThinkBlock, renderCmdList, selectCmd, initMermaid, runMermaid, _mzSt, _mzCardFrom, _mzParts, _mzApply, _mzZoomBy, _mzWheel, _mzDown, _mzPinchPairs, _mzPinchD0, _mzMove, _mzUp, _mzDbl, initMermaidZoom, _mzClickOpen, openMzPreview, closeMzPreview, _mzEsc`
- **23 var**：`toolCardMap@23, _toolContainerEl@28, _sbData@31, FOLLOW_THRESHOLD_PX@102, _followBottom@103, _queuedPending@346, _stream@649, _turnSeq@653, THINK_FLUSH_MS@654, CONTENT_FLUSH_MS@655, commandsCache@929, cmdHighlightIdx@930, _pendingBacktrack@1056, _pendingHistoryRefresh@1059, liveMsgIndex@1060, _mermaidInit@1262, _mzState@1295, _mzMin@1296, _mzDrag@1297, _mzPts@1298, _mzPinch@1299, _mzClick@1300, _mzOpenAt@1301`
- **7 let**（`ui.js:13-19`）：`ws, sessionId, processing, reconnectTimer, lastGroupEl, thinkingEl, progressEl` ← **全局会话状态，app.js 全在读写**
- **7 const**（`ui.js:73-81`）：`messagesEl, inputEl, sendBtn, sessionLabel, modelBadge, statusEl, AUTH_KEY` ← **顶层 DOM 缓存，执行期立即 getElementById**

## 5. DOM 事件绑定与初始化顺序

**隐式时序前提（ESM 改造必须保住的三条）**
1. **DOM 已解析**：`ui.js:73-78` 顶层 `document.getElementById(...)` ×6 **立即执行**——现在靠脚本在 `</body>` 前；改 `type="module"`（自动 defer）仍成立，但**若挪进 `<head>` 且不 defer 会崩**。
2. **ui.js 先于 halo.js**：`halo.js:554-556` 猴补丁要求 `window.setProcessing/showThinking/removeThinking` 已存在（`halo.js:549` `typeof orig === 'function' else return` 有防御，但静默失效）。
3. **DOM 就绪后才拿 DOM**：`proj.js:54-60` IIFE 顶层 `getElementById('halo-canvas'/'proj-layer'/'messagesContainer'/'authOverlay'/'sessionsModal')`。

**各文件执行期副作用**
- `ui.js`：`182` `document.addEventListener('click', handleFileRefClick)`；`983,996,1011` `#cmdList` click/mouseover/mouseleave；`1288` `window.addEventListener('load', initMermaid)`
- `halo.js`（IIFE 顶层立即）：`534-536` 写 window 桥 → `554-556` 猴补丁 → `558` MutationObserver（监听 `<html class>` 主题）→ `562` resize → `569` visibilitychange → `585-588` `syncFromSbData()` + `setHaloEnabled(true)`（**默认开**，读 `localStorage['webui_halo']`）
- `proj.js`（IIFE 顶层立即）：`367-371` scroll/resize → `387` `requestAnimationFrame(tick)` **无限循环启动** → `390` 写 `window.__proj`
- `app.js`：**唯一 DOMContentLoaded** 在 `app.js:1025`，回调内：`isAuthed() ? initApp() : showLogin()` → 绑 sessionsModal 点击、ESC、输入区外点、resize、visibilitychange 续传、iOS touchmove
- `initApp()`（`app.js:1009-1023`）：`hideLogin()` → 恢复主题 `applyTheme` → `applyHalo(window.isHaloEnabled())` → **`connectWebSocket()`** → `inputEl.focus()`

**顺序链**：ui(同步, DOM缓存) → halo(IIFE, 猴补丁+默认开) → proj(IIFE, rAF 启动) → app(注册 DOMContentLoaded) → **DOMContentLoaded → initApp → connectWebSocket**

## 6. 隐式全局读写

**写 window**：
- `halo.js:534-536` `window.setHaloEnabled/isHaloEnabled/setHaloState`
- `halo.js:427-428` `window.__haloLight`（被 proj.js:280 消费）
- `proj.js:390` `window.__proj = { set, stats }`
- `app.js:157` `window._cancelTimeout = cancelTimeout`（自用，`:443` 消费）

**读 window**：`app.js:987,988,1001,1002,1018,1019`（halo API，带 typeof 防御）；`proj.js:280,281`（halo API）；`ui.js:1470` `window.getSelection`

**`globalThis`：零命中。** 严格模式：`halo.js:23`、`proj.js:21` 有 `'use strict'`；**app.js / ui.js 无**（非严格）。

**真隐式全局赋值：零。** 静态扫描命中的 19 处全是**参数/局部变量重赋值**（例：`app.js:81 opts = opts || {}` 是 `withAuth(opts)` 形参；`ui.js:1349 ns = Math.min(...)` 是 `_mzZoomBy(c,ns,ax,ay)` 形参；`halo.js:314 radial = true` 对应 `halo.js:312 var p0,p1,p2,p3,radial`）。
**唯一全局重绑定**：`app.js:937` `switchSession = function(sid){…}`（对已声明全局的重赋值，ESM 下语义会变——见 §7）。

## 7. ESM 拆分建议（8 模块）

```
static/
├─ index.html          <script type="module" src="/static/main.js">
├─ core/
│  ├─ dom.js           — DOM 缓存(messagesEl/inputEl/sendBtn/sessionLabel/modelBadge/statusEl) + AUTH_KEY
│  ├─ state.js         — store：ws/sessionId/processing/_sbData/_stream/_turnSeq/… + 订阅/派发
│  ├─ http.js          — withAuth/authFetch + getToken/setToken/clearToken/isAuthed
│  └─ util.js          — escapeHtml/formatTime/scrollToBottom/autoResize/showError/setProcessing
├─ render/…            — 消息与流式渲染（现 ui.js 130-926）
├─ panel/cmd.js        — 命令面板（现 ui.js 927-1053）
├─ panel/history.js    — 会话历史渲染（现 ui.js 1054-1260）
├─ decor/halo.js       — 现 halo.js（去 IIFE，export setHaloEnabled 等）
├─ decor/proj.js       — 现 proj.js（去 IIFE）
├─ app/…               — 认证/WS/会话/发送（现 app.js）
└─ main.js             — 入口：绑 DOMContentLoaded，import 顺序 = 现 §5 顺序
```

**建议 8 个模块**：`core/dom · core/state · core/http · core/util · render · panel/cmd+history(合并) · decor/halo+proj(合并或各自) · app · main`（若 decor 分开则 9-10）。

### 各模块 export/import（关键边）
| 模块 | export | import |
|---|---|---|
| `core/dom.js` | `messagesEl, inputEl, sendBtn, sessionLabel, modelBadge, statusEl` | — |
| `core/state.js` | `get/set/subscribe`（或 `{ws, sessionId, processing, _sbData, …}`）| — |
| `core/http.js` | `authFetch, withAuth, getToken, setToken, clearToken, isAuthed` | `core/state` |
| `core/util.js` | `escapeHtml, formatTime, scrollToBottom, showError, setProcessing, showThinking, removeThinking, updateStatusBar` | `core/dom`, `core/state` |
| `render/*` | `addUserMessage, addAssistantMessage, addToolCall, addToolResult, finalizeStreamRound*, renderHistoryMessages, clearUI, …` | `core/*` |
| `panel/cmd.js` | `toggleCmdPalette, closeCmdPalette, navigateCmdList, confirmCmdSelection` | `core/http`(**authFetch**), `core/dom` |
| `decor/halo.js` | `setHaloEnabled, isHaloEnabled, setHaloState` | `core/state`(**_sbData**) |
| `app/*` | `sendMessage, cancelMessage, doLogin, initApp, …` | 几乎全部 |
| `main.js` | —（副作用入口） | 全部 + 注册 DOMContentLoaded |

### 循环依赖风险点与解法
1. **`ui ↔ app`（最大风险，48 vs 3）**
   - 现状：app 重度依赖 ui；ui 仅 3 处反向（`authFetch`/`cancelMessage`/`sendMessage`）。
   - **解法：依赖反转** —— `authFetch` 下沉到 `core/http.js`（ui 不再 import app）；`cancelMessage/sendMessage` **不在 ui 里 import**，改为在 `core/state.js` 注册 `sendHandler/cancelHandler`（app 启动时 `state.setHandler('send', sendMessage)`），ui 只读 handler 调用。**彻底消除 A↔B**。
2. **`decor/halo → render`（猴补丁）**
   - 现状：`halo.js:554-556` 运行时改写 `window.setProcessing/showThinking/removeThinking`。
   - **解法：事件订阅** —— `core/state.js` 在 `state` 变化时 `notify()`，halo 订阅 `subscribe(s => setHaloState(s))`；删掉 `wrap()`。**这正是 n5 Store/Reducer 的副产品**。
3. **`app ↔ decor/halo`（window 桥）**
   - 现状：`app.js:987-1019` 读 `window.setHaloEnabled/isHaloEnabled`。
   - **解法：直接 import** —— `import { setHaloEnabled, isHaloEnabled } from './decor/halo.js'`；`window.__haloLight` 保留（proj 跨模块共享的渲染缓存，可改为 `core/light.js` 导出单例）。
4. **`app.js:937 switchSession` 全局重绑定**
   - ESM 下 `function switchSession` 是模块局部，`switchSession = function` 会抛 `Assignment to constant`（若改 const）或只改模块内引用。
   - **解法：合并定义**，直接删除 `app.js:781` 旧版，用新实现单点定义；`_origSwitchSession@936` 若无人引用则一并删（已确认无外部引用）。

### 必须保留为 `window.*` 的全局
**A. index.html 内联事件属性调用的 15 个**（HTML 属性在全局作用域求值，ESM 模块函数**不**自动挂 window）：
`doLogin(app.js:110), openSessions(684), newAgent(858), reloadAgent(835), clearSession(844), toggleHalo(1000), toggleTheme(973), onFileInputChange(510), handleKeyDown(636), sendMessage(577), onSessionsSearchInput(732), closeSessions(693)` ＋ `autoResize(ui.js:120), handleInputFocus(ui.js:126), toggleCmdPalette(ui.js:932)`
> **更优解**：在 `main.js` 里改用 `addEventListener` 绑定（HTML 属性全部删掉），即可**不保留任何 window 全局**，且顺带消除内联 XSS 面。若要最小改动，则在 `main.js` 末尾 `Object.assign(window, {doLogin, openSessions, …})` 集中一处声明。

**B. 控制台调试入口（建议保留）**：`window.__proj`（`proj.js:390`）、`window.__haloLight`（`halo.js:428`，proj 读）

**C. 可随 import 化删除**：`window.setHaloEnabled/isHaloEnabled/setHaloState`（`halo.js:534-536`）、`window._cancelTimeout`（`app.js:157`，改模块内变量）、halo 的 `wrap()` 三件套

**D. CDN 全局（保持不动）**：`marked, DOMPurify, mermaid, katex, renderMathInElement` —— 由 `index.html:17-25` 的 defer script 提供，**ESM 模块脚本在它们之后执行**（module 天然 defer 且按文档序），故 `renderMarkdown/ initMermaid` 等直接读全局是安全的；如需严谨可加 `window.marked` 断言。
