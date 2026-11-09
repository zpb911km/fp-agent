/* ═══════════════════════════════════════════════════════════
   FP WebUI — WebSocket 连接生命周期（状态/取消/连接/重连）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { ws, setWs, reconnectTimer, setReconnectTimer, statusEl, sendBtn, updateStatusBar, setProcessing, setSb } from '../ui.js';
import { getToken } from '../core/http.js';
import { dispatch } from '../core/events.js';
import { store } from '../core/store.js';
import { loadSessionsList } from './sessions.js';

// 连接期共享状态：wire.js(pong/connected)、本模块、app.js(visibilitychange)
// 共读写 → 单一可变对象导出（ESM 导入绑定不可重新赋值，let 跨模块不够用）
export const conn = {
  lastPingTime: null,  // ping 发出时刻（延迟测量）
  pingInterval: null,  // 10s 心跳定时器
  lastSeq: 0,          // 已消费最大事件 seq（重连回传做重放协商）
  runId: '',           // 服务端 run_id（重启后变化 → 旧 seq 作废）
  wakeProbe: null      // 唤醒存活探测定时器
};


// ── 工具函数 ──
export function cancelMessage() {
  if (!ws || ws.readyState !== WebSocket.OPEN) return;
  if (!store.getState().processing) return;

  // 发送取消请求 → 后端 task.cancel() 注入 CancelledError
  ws.send(JSON.stringify({ type: 'cancel' }));

  // 按钮临时禁用，等待后端 cancelled 事件返回后再恢复
  sendBtn.disabled = true;
  sendBtn.textContent = '⏳';
  sendBtn.style.background = '#555';
  sendBtn.style.borderLeftColor = '#555';

  // 安全网：5 秒后如果后端没响应，强制恢复
  var cancelTimeout = setTimeout(function() {
    if (store.getState().processing) setProcessing(false);
  }, 5000);
  // 存储 timeout id 以便 cancelled 事件到达时清理
  window._cancelTimeout = cancelTimeout;
}

export function connectWebSocket() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) return;

  var protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  var host = window.location.host;
  var token = getToken();
  // 携带已消费序号与 run_id：后端据此决定重放（replay）还是全量重拉（resync）
  var wsUrl = protocol + '//' + host + '/ws/chat?token=' + encodeURIComponent(token) +
    '&seq=' + conn.lastSeq + '&run_id=' + encodeURIComponent(conn.runId);

  setWs(new WebSocket(wsUrl));

  ws.onopen = function() {
    statusEl.textContent = '🟢 已连接';
    setProcessing(false);
    if (reconnectTimer) {
      clearTimeout(reconnectTimer);
      setReconnectTimer(null);
    }
    // 启动延迟探测（每 10 秒 ping）
    if (conn.pingInterval) clearInterval(conn.pingInterval);
    conn.pingInterval = setInterval(function() {
      try {
        conn.lastPingTime = Date.now();
        ws.send(JSON.stringify({ type: 'ping' }));
      } catch(e) {}
    }, 10000);
    loadSessionsList();
  };

  ws.onclose = function() {
    statusEl.textContent = '🔴 已断开';
    setProcessing(false);
    setSb({ latency: null });
    updateStatusBar();
    if (conn.pingInterval) { clearInterval(conn.pingInterval); conn.pingInterval = null; }
    if (!reconnectTimer) {
      setReconnectTimer(setTimeout(function() {
        setReconnectTimer(null);
        connectWebSocket();
      }, 3000));
    }
  };

  ws.onerror = function() {
    statusEl.textContent = '🔴 连接错误';
  };

  ws.onmessage = function(event) {
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