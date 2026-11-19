/* ═══════════════════════════════════════════════════════════
   FP WebUI — 会话管理（列表/搜索/切换/删除）
   ═══════════════════════════════════════════════════════════ */
// @ts-nocheck

import { escapeHtml, clearUI, sessionLabel, scrollToBottom, showError } from '../ui.js';
import { authFetch } from '../core/http.js';
import { fetchSessionHistory } from './history.js';
import { LoadingState } from '../core/spinner.js';

// ── 会话管理 ──

/* 主卡展示的关键词个数：**由后端下发**（fp_core.core.session_keywords.TOP_SHOW，
   经 /api/sessions 的 kw_show 字段），前端不再写死。
   曾写死 3 而后端是 5，结果终端显示 5 个、webui 只显示 3 个——同一份数据两处
   各存一份，改一处必漂另一处。兜底值仅首帧未拿到响应时用。 */
var _kwShow = 5;

/* 重命名输入框的字数上限：同样**由后端下发**（= fp_core.core.session.SUMMARY_LIMIT），
   前端不写死——写死必漂（kw 展示个数就漂过一次：终端 5 个、webui 3 个）。
   兜底值仅在首帧未拿到响应时用。 */
var _summaryLimit = 20;

/* 侧边栏开合语义（重构后：会话列表常驻侧边栏，不再是浮层）：
   - 宽屏(≥900px)：侧边栏常驻，openSessions=确保展开+聚焦搜索；closeSessions=无操作
     （切换会话/Esc 不再把宽屏侧边栏关掉，DeepSeek 行为）
   - 窄屏(<900px)：抽屉式，open/close 控制滑入滑出 + 遮罩 + 背景滚动锁 */
var _sidebarMq = window.matchMedia('(min-width: 900px)');

function isWideScreen() { return _sidebarMq.matches; }

function sidebarEl() { return document.getElementById('sidebar'); }
function scrimEl() { return document.getElementById('sidebarScrim'); }

export function openSessions() {
  var sb = sidebarEl();
  var si = document.getElementById('sessionsSearchInput');
  if (!isWideScreen()) {
    sb.classList.add('open');
    var sc = scrimEl();
    if (sc) sc.hidden = false;
    document.body.style.overflow = 'hidden';
  } else {
    sb.classList.remove('collapsed');   // 宽屏：确保展开
  }
  if (si) si.value = '';
  loadSessionsList();
  if (si && isWideScreen()) si.focus();
}

export function closeSessions() {
  if (isWideScreen()) return;           // 宽屏常驻，无关闭语义
  var sb = sidebarEl();
  if (sb) sb.classList.remove('open');
  var sc = scrimEl();
  if (sc) sc.hidden = true;
  document.body.style.overflow = '';
}

/** 折叠/展开（侧边栏头部 « 钮）：宽屏折叠宽度、窄屏等价开合 */
export function toggleSidebar() {
  var sb = sidebarEl();
  if (!sb) return;
  if (isWideScreen()) {
    sb.classList.toggle('collapsed');
  } else if (sb.classList.contains('open')) {
    closeSessions();
  } else {
    openSessions();
  }
}

/* 视口跨过 900px 断点时清掉另一侧残留态：
   窄→宽：抽屉开着但宽屏不需要 open/遮罩；宽→窄：折叠类在窄屏无语义 */
function _onSidebarMqChange() {
  var sb = sidebarEl();
  if (!sb) return;
  if (isWideScreen()) {
    sb.classList.remove('open');
    var sc = scrimEl();
    if (sc) sc.hidden = true;
    document.body.style.overflow = '';
  } else {
    sb.classList.remove('collapsed');
  }
}
if (_sidebarMq.addEventListener) _sidebarMq.addEventListener('change', _onSidebarMqChange);
else if (_sidebarMq.addListener) _sidebarMq.addListener(_onSidebarMqChange);

/** 单条会话 DOM（列表与搜索结果共用；搜索结果额外带正文片段） */
function renderSessionItem(s, withSnippets) {
  var isCurrent = s.is_current;
  var msgCount = s.message_count || 0;
  // 后端已保证 summary 可展示；此处仅兜底——不用 sid 冒充标题
  var summary = s.summary || (msgCount ? '（无摘要）' : '（空白会话）');
  // 关键词由后端 KL 管线预先算好（meta.kw），前端只读不算
  var kw = (s.kw && s.kw.length) ? s.kw.slice(0, _kwShow).join(' · ') : '';
  var snippets = '';
  if (withSnippets) {
    (s.snippets || []).forEach(function(sn) {
      snippets += '<div class="session-item-snippet">' +
        '<span class="snip-role">' + (sn.role === 'user' ? '👤' : '🤖') + '</span>' +
        escapeHtml(sn.text) + '</div>';
    });
  }
  /* 主卡只留 summary + kw + ⋯（验收⑥）；日期/meta/操作全收二级菜单。
     搜索片段(snippets)留主卡：它是命中确认信息，收进菜单会让搜索失去
     「为何命中」的即时反馈（取舍已写入报告）。
     菜单 meta 分层（打磨②）：日期(创建/更新) → 消息数 → token 明细 → kw 全量，
     逐行成层。token 明细由后端 /api/sessions 的 token_usage 摘要下发
     （rows=成品行：图标/数值/标签/悬浮说明，含总计/输入未命中/输入命中/
     输出/命中率），前端只插值——口径不在这边写第二份。 */
  var kwAll = (s.kw && s.kw.length) ? s.kw.join(' · ') : '';
  function metaRow(icon, value, label, tip) {
    return '<div class="sim-row"><span class="sim-k">' + icon + '</span>' +
      '<span class="sim-v"' + (tip ? ' title="' + escapeHtml(tip) + '"' : '') + '>' +
      escapeHtml(value) + '</span>' +
      '<span class="sim-l">' + escapeHtml(label) + '</span></div>';
  }
  var metaRows = '';
  if (s.created) metaRows += metaRow('📅', String(s.created).slice(0, 16), '创建');
  if (s.updated) metaRows += metaRow('🕐', String(s.updated).slice(0, 16), '更新');
  metaRows += metaRow('💬', String(msgCount), '条消息');
  /* token 消耗：无用量（未调用过 LLM 的会话 / 只在退出时结算过的老会话）→ 不渲染，
     不造 "0 tokens" 的假数据。rows 为后端成品行；short/detail 是兼容旧载荷的
     单行退化路径（跨版本热更时前端新、后端旧也不会空着）。 */
  var tk = s.token_usage;
  if (tk && tk.rows && tk.rows.length) {
    tk.rows.forEach(function(r) { metaRows += metaRow(r.k || '🪙', r.v, r.l || '', r.tip); });
  } else if (tk && tk.short) {
    metaRows += metaRow('🪙', tk.short, 'tokens', tk.detail);
  }
  if (kwAll) metaRows += metaRow('🏷', kwAll, '关键词');
  return '<div class="session-item' + (isCurrent ? ' current' : '') + '" data-sid="' + s.id + '"' +
      ' data-locked="' + (s.summary_locked ? '1' : '') + '">' +
    '<div class="session-item-info">' +
      '<div class="session-item-summary">' + escapeHtml(summary) + '</div>' +
      (kw ? '<div class="session-item-kw">' + escapeHtml(kw) + '</div>' : '') +
      snippets +
    '</div>' +
    '<button class="session-more-btn" title="更多操作" aria-label="更多操作"' +
      ' aria-expanded="false" aria-haspopup="true">⋯</button>' +
    '<div class="session-item-menu" hidden>' +
      (isCurrent ? '<span class="session-item-badge">当前</span>' : '') +
      '<div class="session-item-meta">' + metaRows + '</div>' +
      '<div class="session-item-actions">' +
        '<button class="session-load-btn">' +
          (isCurrent ? '✓ 当前' : '切换到') +
        '</button>' +
        '<button class="session-rename-btn" title="重命名会话（改名后不会被自动摘要覆盖）"' +
          ' aria-label="重命名会话">✎ 重命名</button>' +
        (!isCurrent ? '<button class="session-del-btn" title="删除会话" aria-label="删除会话">🗑</button>' : '') +
        '<button class="session-kw-btn" title="重算该会话关键词（本地 KL 管线，零 LLM）"' +
          ' aria-label="重算关键词">⟳ 重算 kw</button>' +
      '</div>' +
    '</div>' +
  '</div>';
}

var _sessionsSearchTimer = null;

export function onSessionsSearchInput() {
  var el = document.getElementById('sessionsSearchInput');
  var q = (el && el.value ? el.value : '').trim();
  clearTimeout(_sessionsSearchTimer);
  if (!q) { loadSessionsList(); return; }
  _sessionsSearchTimer = setTimeout(function() { runSessionsSearch(q); }, 250);
}

/** 搜正文（与终端 /find 同一套后端语义） */
function runSessionsSearch(q) {
  var list = document.getElementById('sessionsList');
  authFetch('/api/sessions/search?q=' + encodeURIComponent(q))
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.kw_show) _kwShow = data.kw_show;
      if (data.summary_limit) _summaryLimit = data.summary_limit;
      var results = data.results || [];
      if (!results.length) {
        list.innerHTML = '<div class="no-sessions">没有找到含「' + escapeHtml(q) + '」的会话</div>';
        return;
      }
      var html = '<div class="search-summary">『' + escapeHtml(q) + '』命中 ' + results.length + ' 个会话</div>';
      results.forEach(function(s) { html += renderSessionItem(s, true); });
      list.innerHTML = html;
    })
    .catch(function(err) {
      list.innerHTML = '<div class="no-sessions">搜索失败: ' + escapeHtml(err.message) + '</div>';
    });
}

export function loadSessionsList() {
  var list = document.getElementById('sessionsList');
  LoadingState.show(list, '载入会话列表…');
  return authFetch('/api/sessions')
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.kw_show) _kwShow = data.kw_show;
      if (data.summary_limit) _summaryLimit = data.summary_limit;
      var sessions = data.sessions || [];
      if (sessions.length === 0) {
        list.innerHTML = '<div class="no-sessions">暂无历史会话</div>';
        LoadingState.hide(list);
        return;
      }
      var html = '';
      sessions.forEach(function(s) {
        html += renderSessionItem(s, false);
      });
      list.innerHTML = html;
      LoadingState.hide(list);
    })
    .catch(function(err) {
      list.innerHTML = '<div class="no-sessions">加载失败: ' + err.message + '</div>';
      LoadingState.hide(list);
    });
}

/* ── 刷新（顶栏/下拉共用）：真语义 = 触发当前会话 kw 重算 → 再重载列表
   （重算后新 kw 立即可见）。重算端点为 core KL 管线**纯本地统计、零 LLM**、
   单会话约 18ms → 同步 POST 即可；失败（如新会话无文件）不阻塞列表刷新。
   画风对齐 spinner.css 的 fp-spin：刷新期间 #refreshBtn 图标加 .spinning。
   进行中重复触发直接忽略，防并发重绘把指示态打乱。 ── */
var _refreshing = false;

export function refreshSessions() {
  if (_refreshing) return Promise.resolve();
  _refreshing = true;
  var icon = document.querySelector('#refreshBtn .refresh-icon');
  if (icon) icon.classList.add('spinning');
  return authFetch('/api/sessions/refresh', { method: 'POST' })
    .catch(function() { return null; })          // 重算失败不阻塞列表刷新
    .then(function() { return loadSessionsList(); })
    .finally(function() {
      _refreshing = false;
      if (icon) icon.classList.remove('spinning');
    });
}

/** 单卡菜单「⟳ 重算 kw」：重算该会话关键词后重载列表 */
function recomputeKw(sid, btn) {
  if (btn) { btn.disabled = true; btn.textContent = '…'; }
  authFetch('/api/sessions/' + sid + '/refresh', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function() { return loadSessionsList(); })
    .catch(function(err) {
      if (btn) { btn.disabled = false; btn.textContent = '⟳ 重算 kw'; }
      showError('重算关键词失败: ' + err.message);
    });
}

/* ── 单卡菜单「✎ 重命名」：卡片内联编辑（不弹 window.prompt） ──
   为什么内联：prompt 是阻塞式原生框，移动端观感割裂，且拿不到 maxlength
   与实时边界提示；内联 input 还能复用卡片位置，眼睛不用跳。

   交互：✎ → summary 行就地变 input → Enter 保存 / Esc 取消 / 失焦保存。
   **只改这一条卡片**，不重绘整个列表——重绘会吃掉其它卡片的展开态、
   以及正在编辑的另一条。空值保存 = 撤销人工命名（后端解锁回自动摘要）。

   预填策略：只有**人工命名过**的会话才预填当前名字（便于微调）；自动摘要
   是句子片段（「帮我定位这个 bug」），当名字用反而要先删，故留空。
   判断依据是 data-locked（纯布尔，不把名字塞进 HTML 属性——省掉一轮
   属性转义，避免引号破坏 DOM）。 */
function beginRename(item, btn) {
  if (!item || item.querySelector('.session-rename-input')) return;
  var line = item.querySelector('.session-item-summary');
  if (!line) return;

  var sid = item.dataset.sid;
  var shown = line.textContent || '';
  var initial = item.dataset.locked === '1' ? shown : '';

  // 收起该卡菜单：编辑态让 input 独占视线（菜单展开时卡片很高，易错位）
  var menu = item.querySelector('.session-item-menu');
  var more = item.querySelector('.session-more-btn');
  if (menu) menu.hidden = true;
  if (more) more.setAttribute('aria-expanded', 'false');

  var input = document.createElement('input');
  input.type = 'text';
  input.className = 'session-rename-input';
  input.maxLength = _summaryLimit;
  input.value = initial;
  input.placeholder = '会话名称（清空保存 = 恢复自动摘要）';
  input.setAttribute('aria-label', '会话名称');
  input.title = '最多 ' + _summaryLimit + ' 字；清空保存 = 恢复自动摘要';
  line.textContent = '';
  line.appendChild(input);
  input.focus();
  input.select();

  var closed = false;   // 防「Enter 保存 + 随后的 blur」双触发（Esc 同理）
  function restore(text) { closed = true; line.textContent = text; }

  function save() {
    if (closed) return;
    var value = input.value.trim();
    if (value === initial.trim()) { restore(shown); return; }   // 没改：不打扰后端
    closed = true;
    input.disabled = true;            // 提交中：禁止继续输入
    authFetch('/api/sessions/' + sid + '/rename', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ summary: value }),
    })
      .then(function(r) { return r.json(); })
      .then(function(data) {
        if (data.status !== 'renamed') throw new Error(data.detail || '未知错误');
        // 后端回的是**生效值**（已规范化/截断；撤销时是重新派生的自动摘要）
        line.textContent = data.summary || '（无摘要）';
        item.dataset.locked = data.renamed ? '1' : '';
      })
      .catch(function(err) {
        closed = false;
        input.disabled = false;
        line.textContent = shown;     // 回滚到编辑前
        showError('重命名失败: ' + err.message);
      });
  }

  input.addEventListener('keydown', function(e) {
    if (e.key === 'Enter') {
      e.preventDefault();
      e.stopPropagation();            // 别让全局 Enter 快捷键（发送消息等）抢走
      save();
    } else if (e.key === 'Escape') {
      e.preventDefault();
      e.stopPropagation();
      restore(shown);                 // 取消：恢复原名
    }
  });
  input.addEventListener('blur', save);
}

/* ── 下拉刷新（#sessionsList 到顶后继续下拉 → 同一 refreshSessions 链路）──
   桌面主路径 = 滚轮：到顶后继续向上滚（deltaY<0，等价「把列表往下拉」）
   进入下拉态——这正是顶部边界的自然越界方向，与正常向下浏览零冲突。
   连续累积 100px 触发（≈到顶后「再拉一次」）；顶部静默 ≥250ms 才认新手势，
   挡掉「快速滚到顶的惯性尾巴」误触发；停滚 600ms 收起指示。
   触屏路径 = 手指下拉：到顶后下拖 60px 松手触发，期间 preventDefault 拦
   原生回弹（iOS Safari 无 overscroll-behavior 也稳）。
   鼠标按住拖拽不做：与文本选择/点卡切换冲突，刷新钮覆盖该场景（报告写明）。
   指示走 #pullIndicator（搜索框与列表之间，列表 innerHTML 重绘不吞它）。 */
var PULL_WHEEL_PX = 100, PULL_TOUCH_PX = 60;
var PULL_IDLE_MS = 250, PULL_DECAY_MS = 600;
var _pullList = document.getElementById('sessionsList');
var _pull = { dist: 0, timer: null, lastT: 0, gesture: false, touchY: null };

function pullHide() {
  var el = document.getElementById('pullIndicator');
  if (!el || el.hidden) return;
  el.classList.remove('active', 'ready');
  el.hidden = true;
}

function pullShow(ready) {
  var el = document.getElementById('pullIndicator');
  if (!el) return;
  if (el.hidden) { el.hidden = false; void el.offsetHeight; }  // 强制回流，让高度过渡生效
  var txt = ready ? '⟳ 松开刷新' : '↓ 下拉刷新';
  if (el.textContent !== txt) el.textContent = txt;   // 仅变更时写入，防 aria-live 刷屏
  el.classList.add('active');
  el.classList.toggle('ready', !!ready);
}

function pullEnd() {
  clearTimeout(_pull.timer);
  _pull.dist = 0;
  _pull.gesture = false;
  pullHide();
}

function pullTrigger() {
  pullEnd();
  refreshSessions();
}

_pullList.addEventListener('wheel', function(e) {
  if (_refreshing) { pullEnd(); return; }
  var now = Date.now();
  var sinceLast = now - _pull.lastT;
  _pull.lastT = now;
  if (_pullList.scrollTop > 0) { pullEnd(); return; }
  var dy = e.deltaY;
  if (e.deltaMode === 1) dy *= 16;             // Firefox 行模式折算像素
  else if (e.deltaMode === 2) dy *= _pullList.clientHeight;
  if (dy > 0) {                                // 下行方向（正常浏览/抖动）：削减而非清零
    if (!_pull.gesture) return;
    _pull.dist = Math.max(0, _pull.dist - dy);
    if (_pull.dist === 0) return pullEnd();
    pullShow(false);
    clearTimeout(_pull.timer);
    _pull.timer = setTimeout(pullEnd, PULL_DECAY_MS);
    return;
  }
  if (!_pull.gesture) {
    if (sinceLast < PULL_IDLE_MS) return;      // 顶部惯性尾巴：静默不足不认新手势
    _pull.gesture = true;
    _pull.dist = 0;
  }
  _pull.dist += -dy;
  pullShow(false);
  clearTimeout(_pull.timer);
  _pull.timer = setTimeout(pullEnd, PULL_DECAY_MS);
  if (_pull.dist >= PULL_WHEEL_PX) pullTrigger();
}, { passive: true });

_pullList.addEventListener('touchstart', function(e) {
  _pull.touchY = (_pullList.scrollTop <= 0) ? e.touches[0].clientY : null;
  _pull.dist = 0;
}, { passive: true });

_pullList.addEventListener('touchmove', function(e) {
  if (_pull.touchY === null) return;
  if (_refreshing || _pullList.scrollTop > 0) { _pull.touchY = null; pullEnd(); return; }
  var dy = e.touches[0].clientY - _pull.touchY;
  if (dy <= 0) { _pull.dist = 0; pullHide(); return; }   // 手指上移 = 正常滚动方向
  e.preventDefault();                                    // 到顶下拉：拦原生回弹
  _pull.dist = dy;
  pullShow(dy >= PULL_TOUCH_PX);
}, { passive: false });

function pullTouchEnd() {
  if (_pull.touchY === null) return;
  var fire = _pull.dist >= PULL_TOUCH_PX;
  _pull.touchY = null;
  if (fire) pullTrigger(); else pullEnd();
}
_pullList.addEventListener('touchend', pullTouchEnd);
_pullList.addEventListener('touchcancel', pullTouchEnd);

function switchSession(sid) {
  authFetch('/api/sessions/' + sid + '/switch', { method: 'POST' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'switched') {
        clearUI();
        sessionLabel.textContent = '📄 ' + sid;
        closeSessions();
        loadSessionsList();
        fetchSessionHistory(sid);
        var msg = document.createElement('div');
        msg.className = 'msg-group';
        msg.style.borderBottom = 'none';
        // cwd 是会话属性：跨项目切会话会把相对路径基准一起换掉，必须让用户看见
        var dirNote = data.cwd_changed && data.cwd
          ? '<br><span style="color:var(--text-dim);font-size:12px">ℹ️ 工作目录 → ' + escapeHtml(data.cwd) + '</span>'
          : '';
        msg.innerHTML = '<div class="msg" style="justify-content:center;padding:8px 0"><span style="color:var(--text-dim);font-size:12px">📂 已切换到 ' + sid.slice(0, 8) + '... 可发送消息继续对话</span>' + dirNote + '</div>';
        document.getElementById('messagesContainer').appendChild(msg);
        scrollToBottom(true);
      }
    })
    .catch(function(err) { showError('切换会话失败: ' + err.message); });
}

function deleteSession(sid, btnEl) {
  if (!confirm('确定删除会话 ' + sid.slice(0, 12) + '... 吗？此操作不可恢复。')) return;
  btnEl.disabled = true;
  btnEl.style.opacity = '0.3';
  authFetch('/api/sessions/' + sid, { method: 'DELETE' })
    .then(function(r) { return r.json(); })
    .then(function(data) {
      if (data.status === 'deleted') {
        var item = btnEl.closest('.session-item');
        if (item) { item.style.transition = 'opacity 0.3s'; item.style.opacity = '0'; }
        setTimeout(function() {
          if (item) item.remove();
          var list = document.getElementById('sessionsList');
          if (list && list.children.length === 0) {
            list.innerHTML = '<div class="no-sessions">暂无历史会话</div>';
          }
        }, 300);
      } else {
        btnEl.disabled = false;
        btnEl.style.opacity = '';
        showError('删除失败: ' + (data.detail || '未知错误'));
      }
    })
    .catch(function(err) {
      btnEl.disabled = false;
      btnEl.style.opacity = '';
      showError('删除会话失败: ' + err.message);
    });
}
/* ── 列表项动作：事件委托（原内联 onclick 在全局作用域求值，
   ESM 化后 switchSession/deleteSession 不再是 window 符号 → 点击即
   ReferenceError。改为容器委托：容器静态存在于 index.html，
   innerHTML 重绘（列表/搜索）不掉监听；单 handler 内先判按钮再判整卡，
   等价原 stopPropagation 的防双触发语义。⋯ 菜单：点 ⋯ 仅开合菜单、
   点菜单内 meta 区不切换会话，点「切换/删除」走各自动作。）── */
document.getElementById('sessionsList').addEventListener('click', function (e) {
  var more = e.target.closest('.session-more-btn');
  if (more) {
    var menu = more.parentElement.querySelector('.session-item-menu');
    if (menu) {
      var open = menu.hidden;
      // 同列表只允许一个菜单展开（与 DeepSeek 一致，避免堆叠挤压）；
      // 被收起的菜单同步复位其按钮 aria-expanded
      more.closest('#sessionsList')
        .querySelectorAll('.session-item-menu:not([hidden])')
        .forEach(function(m) {
          m.hidden = true;
          var b = m.parentElement.querySelector('.session-more-btn');
          if (b) b.setAttribute('aria-expanded', 'false');
        });
      menu.hidden = !open;
      more.setAttribute('aria-expanded', String(open));
    }
    return;
  }
  // 重命名编辑态：点 input 本身不许触发「点卡切换会话」
  if (e.target.closest('.session-rename-input')) return;
  var del = e.target.closest('.session-del-btn');
  if (del) { deleteSession(del.closest('.session-item').dataset.sid, del); return; }
  var ren = e.target.closest('.session-rename-btn');
  if (ren) { beginRename(ren.closest('.session-item'), ren); return; }
  var kwb = e.target.closest('.session-kw-btn');
  if (kwb) { recomputeKw(kwb.closest('.session-item').dataset.sid, kwb); return; }
  var load = e.target.closest('.session-load-btn');
  if (load) { switchSession(load.closest('.session-item').dataset.sid); return; }
  if (e.target.closest('.session-item-menu')) return;   // 菜单空白/meta 区：不触发切换
  var item = e.target.closest('.session-item');
  if (item && item.dataset.sid) switchSession(item.dataset.sid);
});
