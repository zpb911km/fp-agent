/* ═══════════════════════════════════════════════════════════
   FP WebUI — WebSocket 连接生命周期（状态/取消/连接/重连）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { ws, setWs, reconnectTimer, setReconnectTimer, statusEl, stopBtn, updateStatusBar, setProcessing, setSb } from '../ui.js';
import { getToken } from '../core/http.js';
import { dispatch } from '../core/events.js';
import { store } from '../core/store.js';
import { loadSessionsList } from './sessions.js';
import { LoadingState } from '../core/spinner.js';

// 连接期共享状态：wire.js(pong/connected)、本模块、app.js(visibilitychange)
// 共读写 → 单一可变对象导出（ESM 导入绑定不可重新赋值，let 跨模块不够用）
export const conn = {
  lastPingTime: null,  // ping 发出时刻（延迟测量）
  pingInterval: null,  // 10s 心跳定时器
  lastSeq: 0,          // 已消费最大事件 seq（重连回传做重放协商）
  runId: '',           // 服务端 run_id（重启后变化 → 旧 seq 作废）
  wakeProbe: null,     // 唤醒存活探测定时器
  // ── 连接代际（#228 修复的根）────────────────────────────────
  // 每次 new WebSocket 递增；所有 socket 回调、探针、重连定时器都先比对代际，
  // 过期者一律不改 UI/不调度。这样「旧连接退场」与「新连接建立」的窗口里，
  // 旧 socket 的 onclose 无法把状态打成「已断开」，也无法再排一条重连。
  gen: 0,
  created: 0,            // 累计建连次数（e2e 断言单连接不变式）
  immediateReconnect: false  // CLOSING 窗口里收到重连请求 → 旧连接一退场就 0ms 接上
};

// 摘掉旧 socket 的全部回调：被新连接取代时，旧连接的 close/error 不得再触达状态
function detach(sock) {
  if (!sock) return;
  sock.onopen = null;
  sock.onclose = null;
  sock.onerror = null;
  sock.onmessage = null;
}


// ── 工具函数 ──
export function cancelMessage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!store.getState().processing) return;

  // 发送取消请求 → 后端 task.cancel() 注入 CancelledError
  ws.send(JSON.stringify({ type: 'cancel' }));

  // 只禁中断键（➤ 保持可用）：取消在途时用户正想「停掉重说」，
  // 此时打的新消息应当能排队；cancelled 事件后 setProcessing 会复位本键
  stopBtn.disabled = true;
  stopBtn.textContent = '⏳';

  // 安全网：5 秒后如果后端没响应，强制恢复
  var cancelTimeout = setTimeout(function() {
    if (store.getState().processing) setProcessing(false);
  }, 5000);
  // 存储 timeout id 以便 cancelled 事件到达时清理
  window._cancelTimeout = cancelTimeout;
}

export function connectWebSocket() {
  if (ws) {
    if (ws.readyState === WebSocket.OPEN) return;        // 已有活连接：不重复建
    if (ws.readyState === WebSocket.CONNECTING) return;  // 建连中：不重复建
    // CLOSING/CLOSED：旧连接正在退场/已退场 → 先摘回调再建新的。
    // 不摘的话，旧 socket 的 onclose 会在新连接已就绪之后把 UI 打成
    // 「已断开」并再排一条 3s 重连（用户看到的「连上→断开→又连上」）。
    detach(ws);
  }

  var gen = ++conn.gen;   // 本代编号：本次建连的所有回调只认这一代
  var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  var host = window.location.host;
  var token = getToken();
  // 携带已消费序号与 run_id：后端据此决定重放（replay）还是全量重拉（resync）
  var wsUrl = protocol + '//' + host + '/ws/chat?token=' + encodeURIComponent(token) +
    '&seq=' + conn.lastSeq + '&run_id=' + encodeURIComponent(conn.runId);

  // 场景②：连接/重连过程占位（首连与 3s 定时重连共用本入口）
  LoadingState.show(statusEl, '连接中…');
  var sock = new WebSocket(wsUrl);
  conn.created++;
  setWs(sock);

  sock.onopen = function() {
    if (gen !== conn.gen) return;   // 过期连接：静默
    LoadingState.hide(statusEl);    // 先撤占位再改文本，避免「连接中…已连接」并存
    statusEl.textContent = '🟢 已连接';
    LoadingState.hidePage();   // 场景③收口：首屏启动占位在此结束
    setProcessing(false);
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      setReconnectTimer(null);
    }
    // 启动延迟探测（每 10 秒 ping）
    if (conn.pingInterval) clearInterval(conn.pingInterval);
    conn.pingInterval = setInterval(function() {
      if (gen !== conn.gen) return;
      try {
        conn.lastPingTime = Date.now();
        sock.send(JSON.stringify({ type: 'ping' }));
      } catch(e) {}
    }, 10000);
    loadSessionsList();
  };

  sock.onclose = function() {
    if (gen !== conn.gen) return;   // 过期连接退场：不动状态、不排重连
    // #228：区分「立即重连」与「3s 定时重连」——立即路径只有一帧，
    // 不该喊「3 秒后重连」也不该弹页面级遮罩（那正是用户看到的双重提示闪烁）
    var immediate = conn.immediateReconnect;
    conn.immediateReconnect = false;
    LoadingState.hide(statusEl);
    statusEl.textContent = immediate ? '🟠 重连中' : '🔴 已断开';
    if (!immediate) {
      LoadingState.show(statusEl, '3 秒后重连…');   // 对齐下方 3000ms 重连定时器
      // #227 目标②「同一等待场景只留一处提示」：此处原再叠一层页面级遮罩
      // （"与服务器连接已断开，正在重连…"），与行内 spinner 同屏两处、且遮罩盖住
      // 用户正在读的内容。断联是"背景可恢复事件"，行内状态 + 重连后的系统消息已足够；
      // 页面级遮罩只留给首屏启动（spinner.js 的 showPage）。
    }
    setProcessing(false);
    setSb({ latency: null });
    updateStatusBar();
    if (conn.pingInterval) { clearInterval(conn.pingInterval); conn.pingInterval = null; }
    // 本代连接已死：在途 ping 标记与会话存活探针一律作废（否则探针会把
    // 重连后的新连接当成"半开"误杀——#228 的第二个根因）
    conn.lastPingTime = null;
    if (conn.wakeProbe) { clearTimeout(conn.wakeProbe); conn.wakeProbe = null; }
    if (!reconnectTimer) {
      setReconnectTimer(setTimeout(function() {
        setReconnectTimer(null);
        connectWebSocket();
      }, immediate ? 0 : 3000));
    }
  };

  sock.onerror = function() {
    if (gen !== conn.gen) return;
    statusEl.textContent = '🔴 连接错误';
    LoadingState.show(statusEl, '重试中…');
  };

  sock.onmessage = function(event) {
    if (gen !== conn.gen) return;
    try {
      var data = JSON.parse(event.data);
      // 序号去重：重放/实时事件凡 seq ≤ 已消费序号一律丢弃（防重连重复渲染）
      // connected 豁免：它携带服务端当前 seq 供对齐用，由其分支单独处理，
      // 若在此过滤会把 conn.lastSeq 顶到最新，导致随后的重放事件全被误丢。
      if (data && typeof data.seq === 'number' && data.type !== 'connected') {
        if (data.seq <= conn.lastSeq) return;
        conn.lastSeq = data.seq;
      }
      dispatch(data);
    } catch (e) {
      console.error('[WS] 解析失败:', e);
    }
  };
}

/* 立即重连（唤醒/可见性变化路径的唯一入口）。
   旧连接还处于 CLOSING 时不并行开第二条——只挂号，等它退场后 0ms 接上；
   1.5s 兜底强建，避免 CLOSING 卡住导致永不重连。 */
export function reconnectNow() {
  if (reconnectTimer) { clearTimeout(reconnectTimer); setReconnectTimer(null); }
  if (ws && ws.readyState === WebSocket.CLOSING) {
    conn.immediateReconnect = true;
    var gen = conn.gen;
    setTimeout(function() {
      if (gen !== conn.gen) return;   // 期间已换代（onclose 已接上），无需兜底
      detach(ws);
      connectWebSocket();
    }, 1500);
    return;
  }
  connectWebSocket();
}

/* 休眠唤醒存活探测：可见时立刻探活，5s 无 pong → 只关「本代那条」连接。
   修复点：探针绑定发起时的 socket 与代际，不再用活绑定 ws 去关新连接。 */
export function probeWake() {
  if (!ws || ws.readyState !== WebSocket.OPEN) { reconnectNow(); return; }
  var gen = conn.gen;
  var sock = ws;
  try {
    conn.lastPingTime = Date.now();
    sock.send(JSON.stringify({ type: 'ping' }));
  } catch (e) {}
  if (conn.wakeProbe) clearTimeout(conn.wakeProbe);
  conn.wakeProbe = setTimeout(function() {
    conn.wakeProbe = null;
    if (gen !== conn.gen) return;          // 已换代：旧探针禁止碰新连接
    if (conn.lastPingTime) {               // 本代连接确实没回 pong → 半开，关掉走重连
      try { sock.close(); } catch (e) {}
    }
  }, 5000);
}