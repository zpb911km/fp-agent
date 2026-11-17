/* ═══════════════════════════════════════════════════════════
   FP WebUI — 流式轮
   llm_start→chunk→llm_end 生命周期 + 思考块折叠/终渲染（_stream 模块私有）
   从 ui.js 按职责拆分（组件模块化），2026-11-08
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { setToolContainerEl } from './state.js';
import { scrollToBottom } from './scroll.js';
import { addAssistantMessage, buildThinkBlock } from './messages.js';
import { removeThinking } from './thinking.js';
import { renderMarkdown, renderMath } from './markdown.js';

/* ═══════════════════════════════════════════════════════════
   流式显示 — 思考块 + 正文流式（n3）
   生命周期（一轮 LLM 调用 = llm_start → … → llm_end）：
     llm_start        → startStreamRound()（重置状态，spinner 由 showThinking 管）
     thinking_content → 思考块懒建（展开态）+ 文本累积，50ms 节流 flush
     chunk            → 首块：折叠思考块；正文消息懒建，100ms 节流重渲染 markdown
     stream_end       → flush（后端已先冲刷缓冲再发，顺序安全）
     llm_end/done/…   → finalizeStreamRound()：终渲染 + 清理
   折叠策略：正文到达前思考块展开；首个正文 chunk 到达即折叠为
   「思考 N 字 · Xs」单行（点击可再展开，保留全文）。
   ═══════════════════════════════════════════════════════════ */
var _stream = null;
// 轮次序号：每轮终结（done/cancelled/error）推进一次，流式轮创建时打上当时的
// 序号（_stream.turnSeq）。_stream 终轮**不销毁**（迟到 token 要并入本轮元素），
// 于是它天然会活过轮次边界——没有序号就无法区分「本轮卡片」与「N 轮前的卡片」。
var _turnSeq = 0;
var THINK_FLUSH_MS = 50;    // 思考文本 DOM flush 节流
var CONTENT_FLUSH_MS = 100; // 正文 markdown 重渲染节流

/** 终结当前轮：此后新建的流式轮才算「下一轮」，旧轮卡片不再被终渲染认领 */
export function endStreamTurn() { _turnSeq++; }

/** 现存流式轮是否属于当前这一轮（命令轮没有 llm_start，会撞上上一轮的残留） */
function isCurrentTurnRound() { return !!_stream && _stream.turnSeq === _turnSeq; }

export function startStreamRound() {
  finalizeStreamRound(); // 上一轮残留兜底（flush + 终渲染）
  setToolContainerEl(null); // 新一轮：工具行宿主重置，不复用上一轮卡片
  _stream = {
    turnSeq: _turnSeq,
    thinkStart: 0,           // 首个思考 token 到达时刻：思考时长的起点
    thinkText: '', thinkEl: null, thinkTimer: null, thinkTicker: null, foldAt: 0,
    contentText: '', contentEl: null, contentTimer: null,
    closed: false  // llm_end/done 后置 true：迟到 token 并入本轮，不新建块
  };
}

/* ── 思考块状态行 ──
   展开期「思考中 · N 字 · Xs」随思考逐步走表；折叠后「N 字 · Xs」定格。
   字数一律取 thinkText.length（真实全文），不做 token 长度累加：累加值在迟到
   token 并入正文块之后会与块里实际显示的文本失配（曾出现「思考 482 字」而块内
   实为 565 字）。时长同理——基准取首个思考 token（真正开始思考那刻），不含首字
   延迟 TTFT：标签写的是「思考 Xs」，把模型还没开口的等待算进去就是不实。 */

/** 思考已进行多久；折叠后冻结在折叠时刻 */
function thinkElapsedMs(s) {
  var end = s.foldAt || Date.now();
  return Math.max(0, end - (s.thinkStart || end));
}

/** 把字数/时长写进状态行（前缀由折叠态推导，无需另传参）。
    正文块与状态行同源同刻更新：只刷状态行、正文走 50ms 节流的话，真机上采到过
    「状态行 175 字 / 块内 68 字」的瞬时脱钩。思考正文是纯 textContent（无 markdown
    解析），顺手同步的代价可忽略，换来「眼见字数 = 块内字数」的恒定一致。 */
function renderThinkStatus(s) {
  if (!s || !s.thinkEl) return;
  var statusEl = s.thinkEl.querySelector('.think-msg-status');
  var bodyEl = s.thinkEl.querySelector('.think-msg-body');
  var text = s.thinkText || '';
  if (!statusEl) return;
  if (bodyEl && bodyEl.textContent !== text) {
    bodyEl.textContent = text;
    bodyEl.scrollTop = bodyEl.scrollHeight;
  }
  var folded = s.thinkEl.classList.contains('collapsed');
  statusEl.textContent =
    (folded ? '思考 ' : '思考中 · ') +
    text.length + ' 字 · ' +
    (thinkElapsedMs(s) / 1000).toFixed(1) + 's';
}

/** 思考期走表：文本按 50ms 才 flush，但模型停顿时字数不变、时长仍在走 */
function startThinkTicker(s) {
  stopThinkTicker(s);
  s.thinkTicker = setInterval(function() {
    if (s.foldAt) { stopThinkTicker(s); return; }        // 已折叠：时长定格
    if (!s.thinkEl || !s.thinkEl.isConnected) { stopThinkTicker(s); return; }
    renderThinkStatus(s);
  }, 200);
}

function stopThinkTicker(s) {
  if (s && s.thinkTicker) { clearInterval(s.thinkTicker); s.thinkTicker = null; }
}

/** 折叠思考块为单行摘要（保留全文可展开）。幂等：重复调用只刷新状态行 */
function foldThinkEl(s) {
  if (!s || !s.thinkEl) return;
  if (!s.foldAt) s.foldAt = Date.now();  // 首次折叠定格时长
  s.thinkEl.classList.add('collapsed');
  renderThinkStatus(s);
  stopThinkTicker(s);
}

export function streamThinkToken(token) {
  // 空 token 不是思考内容：provider 常在流首/无思考轮发空帧（reasoning_content=""）。
  // 若照单全收，会在没有思考的轮次里凭空长出「思考 0 字」空块——折叠后留一行噪音，
  // 且把「是否已建块」这个状态误置，后续真实正文到达时折叠的就是这个假块。
  if (!token) return;
  if (!_stream) startStreamRound();
  if (!_stream.thinkEl) {
    // 首个思考 token：spinner 让位给真内容
    removeThinking();
    // 回复卡片先行创建，思考块插在卡片内部（header 与 content 之间），
    // 与历史回看路径同构——不再裸挂 msg-group 下破坏卡片统一性
    if (!_stream.contentEl) _stream.contentEl = addAssistantMessage('');
    // 正文未到：隐藏空的 .msg-content，避免思考期卡片下方出现空白占位
    _stream.contentEl.classList.add('stream-pending');
    _stream.thinkEl = buildThinkBlock('');
    _stream.thinkStart = Date.now();   // 起表：此刻起才算「思考」
    _stream.contentEl.parentNode.insertBefore(_stream.thinkEl, _stream.contentEl);
    renderThinkStatus(_stream);      // 建块即给「思考中 · 0 字 · 0.0s」，不留空状态行
    startThinkTicker(_stream);       // 走表：模型停顿时字数不动，时长照走
    scrollToBottom();
  }
  _stream.thinkText += token;
  if (_stream.closed) {
    // 迟到 token（llm_end 后 fire-and-forget 排序晚到）：直写终帧，不排定时器
    if (_stream.thinkTimer) { clearTimeout(_stream.thinkTimer); _stream.thinkTimer = null; }
    var lateBody = _stream.thinkEl.querySelector('.think-msg-body');
    lateBody.textContent = _stream.thinkText;
    lateBody.scrollTop = lateBody.scrollHeight; // 内部滚动区（40vh 封顶）跟随到底
    renderThinkStatus(_stream);      // 字数补进状态行（时长已随折叠定格）
    return;
  }
  if (_stream.thinkTimer) return;
  _stream.thinkTimer = setTimeout(function() {
    if (!_stream || !_stream.thinkEl) return;
    _stream.thinkTimer = null;
    var body = _stream.thinkEl.querySelector('.think-msg-body');
    body.textContent = _stream.thinkText;
    body.scrollTop = body.scrollHeight; // 内部滚动区跟随到底，最新思考始终可见
    renderThinkStatus(_stream);      // token 到达即刻刷新，不等下一拍走表
    scrollToBottom();
  }, THINK_FLUSH_MS);
}

export function streamContentChunk(chunk) {
  // 空正文不是「正文已到」：provider 会在思考中途发空 content 帧，照单全收会让
  // 思考栏在思考仍在进行时就被折叠（用户看到的就是「思考栏没展开/提前收起」）。
  // 只让步进真实文本的 chunk 承担折叠与建卡。
  if (!chunk) return;
  if (!_stream) startStreamRound();
  // 正文到达 → 思考块折叠（用户决策：正文前展开，正文流式时折叠）
  if (_stream.thinkEl) foldThinkEl(_stream);
  if (!_stream.contentEl) {
    // 复用 addAssistantMessage 的完整消息结构（头像/时间戳/回溯/复制 + liveMsgIndex 记账）
    _stream.contentEl = addAssistantMessage('');
  }
  // 正文首块到达 → 取消空占位隐藏
  _stream.contentEl.classList.remove('stream-pending');
  _stream.contentText += chunk;
  if (_stream.closed) {
    // 迟到 chunk：直写终帧，不排定时器
    if (_stream.contentTimer) { clearTimeout(_stream.contentTimer); _stream.contentTimer = null; }
    _stream.contentEl.innerHTML = renderMarkdown(_stream.contentText);
    return;
  }
  if (_stream.contentTimer) return;
  _stream.contentTimer = setTimeout(function() {
    if (!_stream || !_stream.contentEl) return;
    _stream.contentTimer = null;
    _stream.contentEl.innerHTML = renderMarkdown(_stream.contentText);
    scrollToBottom();
  }, CONTENT_FLUSH_MS);
}

/** 终渲染：flush 残余 + 用权威全文重渲染。
    轮次置 closed 但不销毁——迟到 token（后端 _pub fire-and-forget 排在
    llm_end 之后的末批）并入本轮元素，避免开孤儿思考块。返回是否已显示正文 */
export function finalizeStreamRound(finalContent) {
  if (!_stream) return false;
  var s = _stream;
  s.closed = true;
  if (s.thinkTimer) { clearTimeout(s.thinkTimer); s.thinkTimer = null; }
  if (s.contentTimer) { clearTimeout(s.contentTimer); s.contentTimer = null; }
  if (s.thinkEl) {
    s.thinkEl.querySelector('.think-msg-body').textContent = s.thinkText;
    // 轮次结束一律折叠：纯思考 + 工具调用的轮次根本没有正文 chunk 来触发折叠，
    // 少了这一处，这类卡片的思考栏会一直展开着（用户看到的「不自动折叠」）。
    foldThinkEl(s);
  }
  stopThinkTicker(s);
  // 卡片可能已随 clearUI 作废（重连 resync / 回溯重拉）：此时再写就是写进
  // 脱离文档的"幽灵卡"——写成功了、用户什么也看不到。判为未显示，交回调用方重渲染。
  var shown = !!s.contentEl && document.body.contains(s.contentEl);
  if (shown && finalContent != null) {
    // 权威全文覆盖（llm_end.content / done.final_content）
    s.contentEl.classList.remove('stream-pending');
    s.contentEl.innerHTML = renderMarkdown(finalContent);
    renderMath();
  }
  scrollToBottom();
  return shown;
}

/** done 专用终渲染：只承接**本轮**的流式卡片，并推进轮次序号。

    命令轮（/sc、/task、/memory…）不走 llm_start → startStreamRound 不重置
    _stream，它仍指向上一轮的卡片（终轮不销毁，见上）。若 done 照旧把
    `final_content` 写进去，命令输出的文本就会**覆盖上一轮的正文**：
    卡片看似"被吞掉"、信息缺失，而刷新页面走 REST 历史又完好无损
    （典型症状：/sc 的结果出现在上一轮答案的位置、命令气泡反而在它下面）。
    故：跨轮不写，交回调用方 addAssistantMessage 另起卡片。

    返回 true 表示「本轮正文已由流式卡片承载」（调用方跳过重复渲染）。*/
export function finalizeStreamRoundForDone(finalContent) {
  var sameTurn = isCurrentTurnRound();
  var shown = sameTurn ? finalizeStreamRound(finalContent) : false;
  endStreamTurn();  // 本轮到此终结：下一轮的卡片必须换序号
  return shown;
}

/** clearUI 配套：摘掉流式轮的 DOM 引用（卡片随容器被清空，已成幽灵节点）。

    为什么不能只清容器：`_stream` 是轮次级状态（终轮不销毁，迟到 token 要并入），
    它攥着 thinkEl / contentEl 两个 DOM 引用。`messagesEl.innerHTML = ''` 之后这两个
    引用指向脱离文档的元素，而 streamContentChunk 看到 contentEl 已存在就不再建卡——
    于是后续所有正文都写进无人可见的幽灵卡：**看起来 agent 卡死、其实一直在输出**。
    重连 resync / history_changed 重拉 / agent 忙时切会话，都是这条路径。

    只摘 DOM、**保留累积文本**（thinkText / contentText）：下一个 token 到达时
    `if (!_stream.contentEl)` 会重建卡片，节流 flush 把累积全文一次写进去，
    已生成的内容不会因为清屏而丢。 */
export function detachStreamDom() {
  if (!_stream) return;
  if (_stream.thinkTimer) { clearTimeout(_stream.thinkTimer); _stream.thinkTimer = null; }
  if (_stream.contentTimer) { clearTimeout(_stream.contentTimer); _stream.contentTimer = null; }
  stopThinkTicker(_stream);
  _stream.thinkEl = null;
  _stream.contentEl = null;
}

