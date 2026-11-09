/* ═══════════════════════════════════════════════════════════
   FP WebUI — 历史加载与轮次收尾刷新（回溯/重写两类挂起请求）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { _pendingBacktrack, _pendingHistoryRefresh, setPendingBacktrack, setPendingHistoryRefresh, clearUI, setLiveMsgIndex, renderHistoryMessages } from '../ui.js';
import { authFetch } from '../core/http.js';

// ── 轮次收尾统一刷新：消费「↩ 回溯」与「turn 中被重写」两类挂起请求 ──
// 两者成因不同：回溯路径已在前端 clearUI 过（本地已清空），而重写路径的卡片
// 已与后端落盘失配 → 必须先 clearUI 再全量重拉。合并为一次请求避免重复拉取。
export function flushPendingHistoryRefresh(sid) {
  if (!_pendingBacktrack && !_pendingHistoryRefresh) return;
  var needClear = _pendingHistoryRefresh;
  setPendingBacktrack(false);
  setPendingHistoryRefresh(false);
  if (!sid) return;
  if (needClear) clearUI();
  setTimeout(function() { fetchSessionHistory(sid); }, 100);
}

export function fetchSessionHistory(sid) {
  authFetch('/api/sessions/' + sid + '/messages')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      var msgs = data.messages || [];
      if (msgs.length > 0) {
        // 从尾部找最后一个有效 index（跳过 system 消息的 null index）
        setLiveMsgIndex(0);
        for (var i = msgs.length - 1; i >= 0; i--) {
          if (msgs[i].index != null) {
            setLiveMsgIndex(msgs[i].index);
            break;
          }
        }
      }
      if (msgs.length > 0) {
        renderHistoryMessages(sid, msgs);
      }
    })
    .catch(function(err) { console.warn('[History] 加载历史失败:', err.message); });
}