/* ═══════════════════════════════════════════════════════════
   FP WebUI — 历史加载与轮次收尾刷新（回溯/重写两类挂起请求）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { _pendingBacktrack, _pendingHistoryRefresh, setPendingBacktrack, setPendingHistoryRefresh, clearUI, renderHistoryMessages } from '../ui.js';
import { authFetch } from '../core/http.js';
import { LoadingState } from '../core/spinner.js';

// ── 轮次收尾统一刷新：消费「↩ 回溯」与「turn 中被重写」两类挂起请求 ──
// 两者成因不同：回溯路径已在前端 clearUI 过（本地已清空），而重写路径的卡片
// 已与后端落盘失配 → 必须先 clearUI 再全量重拉。合并为一次请求避免重复拉取。
export function flushPendingHistoryRefresh(sid) {
  if (!_pendingBacktrack && !_pendingHistoryRefresh) return;
  var needClear = _pendingHistoryRefresh;
  setPendingBacktrack(false);
  setPendingHistoryRefresh(false);
  if (!sid) return;
  if (needClear) {
    clearUI();
    // 返工①：clearUI 真空到 100ms 重拉 show 的帧，:empty::after 空态抢跑——先 show 占位
    // （fetchSessionHistory 的 show 幂等接管；回溯路径 needClear=false，clearUI 在按钮侧、
    // 属"turn 收尾等待"排除邻域，不在本次返工面内）。
    LoadingState.show(document.getElementById('messagesContainer'), '重新加载会话内容…');
  }
  setTimeout(function() { fetchSessionHistory(sid); }, 100);
}

// 历史请求代次：只认最新一次（见 fetchSessionHistory）
var _histReqSeq = 0;

export function fetchSessionHistory(sid) {
  // 二次修改①：打开会话拉历史的等待期不再是空白
  //（switchSession/重连重拉/轮次收尾刷新均汇聚于此）
  var mainArea = document.getElementById('messagesContainer');
  if (mainArea) LoadingState.show(mainArea, '加载会话内容…');
  function done() { if (mainArea) LoadingState.hide(mainArea); }   // renderHistoryMessages 若整体替换内容，hide 幂等兜底
  // 请求代次：连通重拉 / 切会话 / history_changed 可能并发（甚至会话不同）。
  // 慢响应的旧请求若照样渲染，会把 A 会话的历史盖到 B 会话视图上——只认最新一次。
  var req = ++_histReqSeq;
  authFetch('/api/sessions/' + sid + '/messages')
    .then(function(r) {
      if (!r.ok) throw new Error('HTTP ' + r.status);
      return r.json();
    })
    .then(function(data) {
      if (req !== _histReqSeq) return;   // 已被更新的请求取代：不碰 DOM、不撤占位
      var msgs = data.messages || [];
      // 编号基准不在这里算：renderHistoryMessages 落地后按「历史末尾 index +
      // 文档序」统一校准实时索引（唯一权威，见 components/messages.js
      // renumberLiveMessages）。此前这里另设一次 setLiveMsgIndex，两处各算一遍，
      // 谁后跑谁说了算——历史在路上时建出的实时卡片没有一处能纠正它。
      if (msgs.length > 0) {
        renderHistoryMessages(sid, msgs);
      }
      done();
    })
    .catch(function(err) {
      if (req !== _histReqSeq) return;
      console.warn('[History] 加载历史失败:', err.message);
      // 返工①：失败≠「加载完成且为空」，不得落入 :empty::after 空态（误导用户
      // "没有消息可回复"）。仅当容器无真实内容时插一行失败提示（占位不计入——
      // done() 随后会撤占位，若按"占位存在即跳过"，撤后真空仍会落入空态）。
      var host = document.getElementById('messagesContainer');
      if (host && host.querySelector(':scope > :not([data-fp-loading])') === null) {
        var line = document.createElement('div');
        line.className = 'msg-group';
        var inner = document.createElement('div');
        inner.className = 'msg';
        inner.style.justifyContent = 'center';
        inner.style.padding = '8px 0';
        var span = document.createElement('span');
        span.style.color = 'var(--text-dim)';
        span.style.fontSize = '12px';
        span.textContent = '⚠️ 会话内容加载失败（' + err.message + '），可切换会话或刷新重试';
        inner.appendChild(span);
        line.appendChild(inner);
        host.appendChild(line);
      }
      done();
    });
}