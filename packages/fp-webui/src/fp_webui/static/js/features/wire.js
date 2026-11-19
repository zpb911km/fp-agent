/* ═══════════════════════════════════════════════════════════
   FP WebUI — WS 事件接线层（on() 注册表，原 27-case switch）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { ws, statusEl, sessionLabel, modelBadge, inputEl, sessionId, messagesEl, clearUI, addSystemMessage, setProcessing, startStreamRound, showThinking, streamThinkToken, streamContentChunk, removeThinking, finalizeStreamRound, finalizeStreamRoundForDone, addAssistantMessage, ensureToolContainer, addToolCall, addToolResult, addQueuedMessage, consumeQueuedBubble, addUserMessage, setLastGroupEl, setPendingBacktrack, setPendingHistoryRefresh, endStreamTurn, setLiveMsgIndex, setSessionId, updateStatusBar, setSb, getOrCreateMsgGroup, scrollToBottom, showError } from '../ui.js';
import { on, dispatch } from '../core/events.js';
import { store } from '../core/store.js';
import { showAskCard, clearAskCard } from './ask.js';
import { flushPendingHistoryRefresh, fetchSessionHistory } from './history.js';
import { LoadingState } from '../core/spinner.js';   // 返工①：connected/重拉间隙的主区占位收场
import { conn } from './ws.js';
import { checkBuild } from '../core/build.js';   // 前端构建戳：本页是否已落后于磁盘

// ── 避免 done.final_content 与 llm_end.content 重复渲染 ──
var _finalContentAlreadyShown = false;

// ── 消息流横幅口径（#227「同一场景只留一处提示」）─────────────────
// 「连接态」（建连中/已断开/已连接/已重载）**只**由状态栏 #connectionStatus
// （spinner + 文本）表达；消息流只承载内容层事实（新消息、工具、错误、任务仍在跑）。
// 历史上这里曾补过两条「重连/重同步」横幅，用户两次指出：spinner 下方再挂一条
// 同义横幅就是重复，于是全部删掉（具体文案见 git 历史与 docs/CHANGELOG.md #227）。
// 一段时期本行注释里仍写着那两句原文，导致 grep / 源码探针反复误报"横幅还在"——
// 故此处只留结论，不复述文案。
// ── WS 事件分发：on() 注册表（原 27-case switch，2026 迁移）──────────
// 每事件一个独立注册，顺序无关；未知类型由 core/events.js 兜底 warn。
// 契约测试从本文件抽取注册，三方不变量保持不变。

on('connected', function (data) {
  // run_id：服务端重启后变化 → 旧 seq 作废，对齐新序号空间
  var wasConnected = !!conn.runId;
  var canReplay = !!data.replay && data.run_id === conn.runId;
  // 本轮重放起点（服务端 main.py「本轮重放」）：非 null 表示服务端随后会把
  // 「本轮至今」的全部事件补发过来——新开页(seq=0)/resync 时轮次还没落盘，
  // REST 历史里没有本轮，只有这批事件能让页面看见本轮提问、已生成正文与工具行。
  var replayBase = (typeof data.replay_base === 'number') ? data.replay_base : null;
  if (data.run_id) conn.runId = data.run_id;
  // 无法重放时把本地序号对齐服务端当前值（对齐放在重放事件到达前，
  // 重放分支保持 conn.lastSeq 不动，让随后到达的重放事件通过去重器）。
  // 有本轮重放时**必须**对齐到 replay_base 而非 data.seq：本轮事件的 seq 全都
  // ≤ 快照点，对齐到 data.seq 会让它们一进 onmessage 就被去重器整批丢掉。
  if (!canReplay) conn.lastSeq = replayBase !== null ? replayBase : (data.seq || 0);

  if (data.session_id) {
    setSessionId(data.session_id);
    sessionLabel.textContent = '📄 ' + data.session_id;
  }
  statusEl.textContent = '🟢 已连接';

  // ── 全量重拉：缓冲溢出或服务端重启，事件流不可续 ──
  // 提示口径见文件顶部：任何「已重连/已同步」横幅都不再出现，连接态交给状态栏。
  var needHistory = false;
  if (data.resync) {
    // 事件流不可续（缓冲溢出/服务端重启）→ 已过去的轮次只能靠历史；
    // 正在跑的本轮由随后的「本轮重放」补（replay_base，见上方）。
    // 必须 clearUI：残留片段与后端已失配，且 seq 空间已换（旧序号无意义）。
    clearUI();
    needHistory = true;
  } else if (wasConnected && !data.processing && sessionId) {
    // 二次修改③：重连成功后必须完全重新请求一次会话内容——
    // 此前仅 resync/DOM空 才重拉，断线重连后 DOM 里残留的片段无法向上翻阅；
    // 现 clearUI + 全量重拉（复用 resync 同款安全路径），processing 中不打断流式（由重放续传）
    clearUI();
    needHistory = true;
  }
  // DOM 为首屏占位/空（首屏/重开页/刚清空）→ 补一次权威历史。
  // 返工①修复：initApp 接力的行内占位本身也是 children——用「非占位子元素」判真空，
  // 否则占位会让 children.length===0 恒假、首拉被跳过（回归陷阱）。
  var hasRealContent = messagesEl.querySelector(':scope > :not([data-fp-loading])') !== null;
  // processing **不再是**跳过条件：需要重拉时容器刚被 clearUI 清空，此时若因
  // "agent 正在跑"跳过，界面就停在空白直到本轮结束——用户实测的「重连后历史不显示、
  // 实时消息又从 #1 重新编号」正是这条路径（编号基准只能由历史对齐，没有历史
  // 就只能从 0 数起，回溯索引整体错位）。
  // 流式与历史不冲突：历史锚定容器最前（components/history.js），流式卡片追加在后。
  if (sessionId && (needHistory || !hasRealContent)) {
    fetchSessionHistory(sessionId);   // 其 show 幂等接管 initApp 接力的占位
  } else {
    // 返工①：无历史可拉的分支必须显式收场——否则 initApp 接力的行内占位永挂。
    // 无 sessionId→撤占位后容器真空，:empty::after 空态才合法显示（真无消息）；
    // processing→留给流式渲染（本就不在 spinner 职责内）。
    LoadingState.hide(messagesEl);
  }

  // ── 运行时快照：任务没死，恢复"处理中"状态机 ──
  // 不再往消息流里补「🔁 后台任务继续运行中」：这条事实已有三处表达——状态栏的
  // 状态位（◎ processing）、➤ 旁亮起 ⏹️ 中断键、以及正在流的卡片本身。用户对"spinner
  // 下方再挂一条同义横幅"明确反感两次，故一律只留**状态栏**这一个出口。
  setProcessing(!!data.processing);
  // ask 等待回复跨连接存续：v2 携带完整元数据 → 恢复整个问答卡片
  // （v2 之前只有布尔，降级为仅恢复输入提示）
  if (data.pending_ask && typeof data.pending_ask === 'object') {
    showAskCard(data.pending_ask);
  } else if (data.pending_ask) {
    inputEl.placeholder = '输入回复...';
    inputEl.focus();
  }

  // 每次连上（含重连）核一次构建戳：agent 热重启会换后端代码，前端若也改过，
  // 本页跑的仍是老模块——这里把"该刷新了"变成状态栏里一个可点的入口。
  checkBuild();

  fetch('/api/health')
    .then(function(r) { return r.json(); })
    .then(function(h) {
      modelBadge.textContent = h.agent || '—';
      sessionLabel.textContent = '📄 ' + (h.session || '—');
      if (h.session) setSessionId(h.session);
    })
    .catch(function() {});
});

on('ping', function (data) {

  try { ws.send(JSON.stringify({ type: 'pong' })); } catch(e) {}
});

on('pong', function (data) {

  if (conn.lastPingTime) {
    setSb({ latency: Date.now() - conn.lastPingTime });
    conn.lastPingTime = null;
    updateStatusBar();
    // 闪烁动画
    var latEl = document.getElementById('sbLatency');
    if (latEl) { latEl.classList.remove('flash'); void latEl.offsetWidth; latEl.classList.add('flash'); }
  }
});

on('llm_start', function (data) {

  startStreamRound();
  showThinking();
});

on('thinking_content', function (data) {

  // 思考 token（后端已批量合并）→ 思考块懒建 + 流式累积
  streamThinkToken(data.token || '');
});

on('chunk', function (data) {

  // 正文 token（后端已批量合并）→ 首块折叠思考块 + 正文流式
  streamContentChunk(data.content || '');
});

on('stream_end', function (data) {

  // 无操作：llm_end 走 finalizeStreamRound 终渲染（后端发 stream_end 前已冲刷缓冲）
});

on('llm_end', function (data) {

  removeThinking();
  // 流式已渲染正文 → 终渲染（权威全文），跳过 addAssistantMessage 双渲染
  var streamed = finalizeStreamRound(data.content);
  if (data.has_tool_calls) {
    if (data.content && !streamed) {
      // LLM 有推理文本 + 工具调用 → 显示文本，后续 tool_select 显示工具选择
      addAssistantMessage(data.content);
    }
    // 纯 tool_calls 无文本 → 由 tool_select 显示选择记录，此处不输出
    // done.final_content 是最终答案，与 llm_end.content 不同 → 标记 false
    _finalContentAlreadyShown = false;
  } else if (data.content) {
    // 纯文本回复，无工具调用
    // done.final_content 与 data.content 相同 → 标记 true，避免 done 重复渲染
    if (!streamed) addAssistantMessage(data.content);
    _finalContentAlreadyShown = true;
  } else {
    // 无正文（纯思考后无内容/异常）：finalizeStreamRound 兜底折叠思考栏（全文可展开）
    finalizeStreamRound();
    _finalContentAlreadyShown = false;
  }
});

on('tool_select', function (data) {

  removeThinking();
  finalizeStreamRound(); // 工具轮可能无 llm_end 正文 → 兜底收尾
  // 方案C：紫卡已移除。工具意图由后续 tool_call 紧凑行表达，
  // 此处只确保本轮有承载工具行的助手卡片
  if (data.tools && data.tools.length > 0) {
    ensureToolContainer();
  }
});

on('tool_call', function (data) {

  removeThinking();
  finalizeStreamRound();
  addToolCall(data.name, data.args, data.tool_call_id);
});

on('tool_result', function (data) {

  addToolResult(data.name, data.result, false, data.tool_call_id);
});

on('queued', function (data) {

  // 运行中排队回执（总线广播）：全设备渲染用户气泡 + ⏳ 已排队标记
  addQueuedMessage(data.content);
});

on('user_message', function (data) {

  // 一切用户消息的显示触发源：直发 / 空闲泵注入（ask 回答、任务终态、
  // 邻居消息）/ 排队消费 / 命令回显（ephemeral 不计入回溯索引）。
  // 不再乐观本地渲染 → 多设备单端口看到同一份消息流，且与历史 REST 一致。
  if (!consumeQueuedBubble(data.content, !!data.ephemeral)) {
    addUserMessage(data.content, !!data.ephemeral);
    setLastGroupEl(null);
  }
});

on('history_changed', function (data) {

  // 上下文被整体重写（改写型命令 /back /sc /clear /fork…，或改写型工具
  // 如 shortcircuit 退化历史块）——后端 msg_count 连续性校验触发。
  // clearUI + 全量重渲染，消除卡片与后端落盘的失配。
  // 与「↩ 回溯」按钮路径互斥：此处接管渲染后取消 done 侧的冗余拉取。
  setPendingBacktrack(false);
  // ── 时机闸门：turn 进行中禁止立刻刷新 ──
  // 此刻前端正在流式渲染当前卡片，clearUI 会把它抹掉；且本轮尚未落盘，
  // 重拉回来的历史里没有它 → 必须等 done/cancelled/error 收尾后再刷。
  if (store.getState().processing) { setPendingHistoryRefresh(true); return; }
  clearUI();
  if (sessionId) {
    // 返工①：clearUI 真空后到 100ms 重拉 show 之间的帧，:empty::after 空态会抢跑——
    // 此刻先 show 占位（fetchSessionHistory 的 show 幂等，label 届时更新）。
    // 收在 sessionId 判空内：无会话时真空即合法空态，不挂占位。
    LoadingState.show(messagesEl, '重新加载会话内容…');
    setTimeout(function() { fetchSessionHistory(sessionId); }, 100);
  }
});

on('queue_error', function (data) {

  // 排队被拒（如运行中斜杠）：只提示，不动 processing 状态
  showError(data.error);
});

on('error', function (data) {

  removeThinking();
  finalizeStreamRound(); // 异常：保留思考/已流式正文最后一帧
  endStreamTurn();       // 异常终结本轮，卡片不再被后续 done 认领
  showError(data.error);
  setProcessing(false);
  flushPendingHistoryRefresh(sessionId);  // 异常也要消费挂起的重写刷新
});

on('done', function (data) {

  removeThinking();
  setProcessing(false);
  clearAskCard(); // 轮次结束时清掉未答复的问答卡片（答复/超时由后端判定）
  // ── 渲染最终回复内容 ──
  // 命令和 LLM 回复统一通过 done.final_content 传递，
  // 不再走独立的 response 事件（参见 commits 71927ab）
  // 去重：纯文本回复（无工具调用）时 llm_end 已渲染过，跳过
  // 命令轮无 llm_start → 这里只承接**本轮**卡片，跨轮走 addAssistantMessage，
  // 否则命令输出会覆盖上一轮答案（吞卡 bug，见 ui.js finalizeStreamRoundForDone）
  // from_command：命令输出不落盘 → ephemeral 渲染（不占索引、无回溯按钮）
  var streamedDone = finalizeStreamRoundForDone(data.final_content);
  if (data.final_content && data.final_content !== '' && !_finalContentAlreadyShown && !streamedDone) {
    addAssistantMessage(data.final_content, !!data.from_command);
  }
  // 流式已渲染正文（极端：llm_end 缺失但 done 有终稿）→ 视为已显示
  if (streamedDone) _finalContentAlreadyShown = true;
  // 重置标志，避免影响下一次
  _finalContentAlreadyShown = false;
  // ── 从后端获取权威索引计数，消除前端自增漂移 ──
  // 每次 done 事件都校准 liveMsgIndex，之后新的消息从正确起点继续累加。
  // 这样即使某条消息在保存过程中被跳过/压缩，索引也不会错位。
  if (data.non_system_count != null) {
    setLiveMsgIndex(data.non_system_count);
  }
  if (data.session_id) {
    setSessionId(data.session_id);
    sessionLabel.textContent = '📄 ' + data.session_id;
  }
  // ── 轮次收尾：消费挂起的「↩ 回溯」或「turn 中被重写」的刷新请求 ──
  flushPendingHistoryRefresh(data.session_id || sessionId);
});

on('cancelled', function (data) {

  removeThinking();
  finalizeStreamRound(); // 中断：保留思考最后一帧/已流式正文
  endStreamTurn();       // 中断终结本轮，卡片不再被后续 done 认领
  // 取消 5 秒安全网定时器
  if (window._cancelTimeout) { clearTimeout(window._cancelTimeout); window._cancelTimeout = null; }
  setProcessing(false);
  flushPendingHistoryRefresh(sessionId);  // 中断也要消费挂起的重写刷新
  addSystemMessage('⏹️ 已中断');
});

on('shutdown', function (data) {

  statusEl.textContent = '🟠 服务器关闭中';
  setProcessing(false);
});

on('reload', function (data) {

  // 「Agent 重建中/即将断开」是**连接态**：口径见文件顶部——只由状态栏说。
  statusEl.textContent = '🟠 Agent 重建中...';
  setProcessing(false);
});

on('reload_done', function (data) {

  statusEl.textContent = '🟢 已连接（已重载）';
  setProcessing(false);
  if (data.session_id) {
    setSessionId(data.session_id);
    sessionLabel.textContent = '📄 ' + data.session_id;
  }
  if (data.model) { modelBadge.textContent = data.model; }
  // 同上：状态栏此刻正是「🟢 已连接（已重载）」——消息流再挂一条 🔄「重载完成」
  // 就是把同一句话说了两遍（用户对这类重复横幅已指出两次）。
});

on('ask', function (data) {

  removeThinking();
  showAskCard(data);
  setProcessing(false);
});

on('info', function (data) {

// 警告此前无 case → 后端发了也静默丢弃，插件抑制 / LLM 重试 / 工具拦截
// 等提示全部看不见。走系统消息通道（与 info 的助手气泡区分开）。
  addAssistantMessage('ℹ️ ' + data.content);
});

on('warning', function (data) {

// 警告此前无 case → 后端发了也静默丢弃，插件抑制 / LLM 重试 / 工具拦截
// 等提示全部看不见。走系统消息通道（与 info 的助手气泡区分开）。

  addSystemMessage('⚠️ ' + data.content);
});

on('hint', function (data) {

// 警告此前无 case → 后端发了也静默丢弃，插件抑制 / LLM 重试 / 工具拦截
// 等提示全部看不见。走系统消息通道（与 info 的助手气泡区分开）。

  var hintGroup = getOrCreateMsgGroup();
  var hintEl = document.createElement('div');
  hintEl.style.cssText = 'font-size:12px;color:var(--text-dim);padding:3px 0 3px calc(clamp(28px, 3vw, 36px) + clamp(8px, 1.5vw, 14px));font-style:italic;';
  hintEl.textContent = '💡 ' + data.content;
  hintGroup.appendChild(hintEl);
  scrollToBottom();
});

on('item', function (data) {

// 警告此前无 case → 后端发了也静默丢弃，插件抑制 / LLM 重试 / 工具拦截
// 等提示全部看不见。走系统消息通道（与 info 的助手气泡区分开）。

  var itemGroup = getOrCreateMsgGroup();
  var itemEl = document.createElement('div');
  itemEl.style.cssText = 'font-size:13px;color:var(--text-secondary);padding:2px 0 2px calc(clamp(28px, 3vw, 36px) + clamp(8px, 1.5vw, 14px));font-family:var(--font-mono);';
  itemEl.textContent = data.content;
  itemGroup.appendChild(itemEl);
  scrollToBottom();
});

on('say', function (data) {

// 警告此前无 case → 后端发了也静默丢弃，插件抑制 / LLM 重试 / 工具拦截
// 等提示全部看不见。走系统消息通道（与 info 的助手气泡区分开）。

  addAssistantMessage(data.content);
});

// ── e2e 测试接缝 ────────────────────────────────────────
// Playwright 经 window.__fp_dispatch 注入事件（test_webui_e2e._dispatch）。
// 分发实现已迁 core/events.js 的 dispatch，接缝名不变 → 测试跨重构稳定。
if (typeof window !== 'undefined') window.__fp_dispatch = dispatch;